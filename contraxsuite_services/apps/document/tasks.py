"""
    Copyright (C) 2017, ContraxSuite, LLC

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

    You can also be released from the requirements of the license by purchasing
    a commercial license from ContraxSuite, LLC. Buying such a license is
    mandatory as soon as you develop commercial activities involving ContraxSuite
    software without disclosing the source code of your own applications.  These
    activities include: offering paid services to customers as an ASP or "cloud"
    provider, processing documents on the fly in a web application,
    or shipping ContraxSuite within a closed source product.
"""
# -*- coding: utf-8 -*-

import csv
import datetime
import io
import json
import os
import re
import tempfile
import zipfile
from shutil import rmtree
from typing import Any, List, Dict, Iterable, Tuple, Union, Optional

import jiphy
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_task_logger
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils.timezone import now
from lexnlp.extract.en.contracts.detector import is_contract
from psycopg2 import InterfaceError, OperationalError

import task_names
from apps.celery import app
from apps.common.collection_utils import chunks
from apps.common.db_cache.db_cache import DbCache
from apps.common.file_storage import get_file_storage
from apps.common.models import ReviewStatus, ExportFile
from apps.common.sql_commons import escape_column_name
from apps.document import signals
from apps.document.async_tasks.detect_field_values_task import DetectFieldValues
from apps.document.constants import DOCUMENT_FIELD_CODE_MAX_LEN, \
    DOC_NUMBER_PER_SUB_TASK, DOC_NUMBER_PER_MAIN_TASK, \
    FieldSpec, DocumentSystemField, DOC_METADATA_DOCUMENT_CLASS_PROB
from apps.document.document_class import DocumentClass
from apps.document.field_detection import field_detection
from apps.document.field_detection.field_detection import detect_and_cache_field_values_for_document
from apps.document.field_detection.field_detection_repository import FieldDetectionRepository
from apps.document.field_detection.regexps_field_detection import CsvDetectorImporter
from apps.document.field_types import TypedField
from apps.document.migration.document_export import DocumentExporter
from apps.document.migration.document_import import DocumentImporter
from apps.document.models import DocumentType, \
    Document, DocumentMetadata, ClassifierModel, TextUnit, DocumentField
from apps.document.models import FieldValue, FieldAnnotation
from apps.document.repository.document_bulk_delete import get_document_bulk_delete
from apps.document.repository.dto import FieldValueDTO
from apps.document.scheme_migrations.scheme_migration import CURRENT_VERSION
from apps.document.signals import fire_documents_status_changed, \
    fire_documents_assignee_changed, fire_document_changed
from apps.document.sync_tasks.document_files_cleaner import DocumentFilesCleaner
from apps.dump.document_type_import import import_document_type
from apps.project.models import Project
from apps.rawdb.field_value_tables import adapt_table_structure
from apps.rawdb.field_value_tables import cache_document_fields
from apps.task.models import Task
from apps.task.tasks import ExtendedTask, CeleryTaskLogger, call_task, Locate
from apps.task.tasks import call_task_func
from apps.task.utils.task_utils import TaskUtils, download_task_attached_file
from apps.users.models import User

__author__ = "ContraxSuite, LLC; LexPredict, LLC"
__copyright__ = "Copyright 2015-2020, ContraxSuite, LLC"
__license__ = "https://github.com/LexPredict/lexpredict-contraxsuite/blob/1.7.0/LICENSE"
__version__ = "1.7.0"
__maintainer__ = "LexPredict, LLC"
__email__ = "support@contraxsuite.com"

MODULE_NAME = __name__
logger = get_task_logger(__name__)


class TrainDocumentFieldDetectorModel(ExtendedTask):
    name = 'Train Document Field Detector Model'
    priority = 7

    def process(self, **kwargs):
        self.log_info(
            'Going to train field detector model based on the datasets stored in DB...')

        document_type = kwargs.get('document_type')

        if document_type:
            self.train_model_for_document_type(document_type['pk'])
        else:
            document_type_pks = DocumentType.objects.values_list('uid', flat=True)
            for document_type_pk in document_type_pks:
                self.train_model_for_document_type(document_type_pk)

    def train_model_for_document_type(self, document_type_pk: str) -> None:
        self.log_info('Building classifier model for document type: {0}'.format(document_type_pk))

        document_type = DocumentType.objects.get(pk=document_type_pk)

        train_model_for_field_args = []

        for field in document_type.fields.all():
            train_model_for_field_args.append((field.uid,))

        self.run_sub_tasks('Train Model For Each Field',
                           TrainDocumentFieldDetectorModel.train_model_for_field,
                           train_model_for_field_args)

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=6000,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
                 max_retries=3)
    def train_model_for_field(task: ExtendedTask, field_uid: str) -> None:
        field = DocumentField.objects.get(pk=field_uid)
        new_model = field_detection \
            .train_document_field_detector_model(CeleryTaskLogger(task), field, None)  # type: ClassifierModel
        if new_model:
            ClassifierModel.objects.filter(document_field=field).delete()
            new_model.save()


class TrainDirtyDocumentFieldDetectorModel(ExtendedTask):
    name = 'Train Dirty Document Field Detector Model'

    def process(self, **kwargs):
        self.log_info(
            'Going to train dirty field detector model based on the datasets stored in DB...')

        dirty_fields = DocumentField.objects.get_dirty_fields()

        if dirty_fields:
            train_model_for_dirty_field_args = []

            for dirty_field in DocumentField.objects.get_dirty_fields():
                train_model_for_dirty_field_args.append((dirty_field.pk,))

            self.run_sub_tasks('Train Model For Dirty Fields',
                               TrainDirtyDocumentFieldDetectorModel.train_model_for_dirty_field,
                               train_model_for_dirty_field_args)

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=6000,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
                 max_retries=3)
    def train_model_for_dirty_field(task: ExtendedTask, dirty_field_id: Any) -> None:
        dirty_field = DocumentField.objects.get(pk=dirty_field_id)
        if dirty_field.can_retrain():
            dirty_field.dirty = False
            dirty_field.save()
            fd_repo = FieldDetectionRepository()
            train_docs_count = fd_repo.get_approved_documents_number(dirty_field, None)
            if train_docs_count >= dirty_field.trained_after_documents_number:
                new_model = field_detection.train_document_field_detector_model(CeleryTaskLogger(task),
                                                                                dirty_field,
                                                                                None)
                if new_model:
                    ClassifierModel.objects.filter(document_field=dirty_field).delete()
                    new_model.save()


@app.task(name=task_names.TASK_NAME_RETRAIN_DIRTY_TASKS, bind=True)
def retrain_dirty_fields(_celery_task):
    TaskUtils.prepare_task_execution()
    if DocumentField.objects.has_dirty_fields():
        task_name = TrainDirtyDocumentFieldDetectorModel.name
        execution_delay = now() - datetime.timedelta(
            seconds=settings.RETRAINING_TASK_EXECUTION_DELAY_IN_SEC)
        if not Task.objects.active_tasks_exist(task_name, execution_delay):
            call_task(task_name, module_name='apps.document.tasks')


@app.task(name=task_names.TASK_NAME_CACHE_UPDATED_DOCS, bind=True)
def cache_updated_documents(_celery_task):
    # TODO: read these settings from AppVar
    task_limit_seconds = 10
    task_query_minutes = 1

    TaskUtils.prepare_task_execution()

    time_threshold = datetime.datetime.now() - datetime.timedelta(minutes=task_query_minutes)
    dirty_documents = Document.objects.filter(fields_dirty__lt=time_threshold).order_by('-fields_dirty')
    dirty_count = dirty_documents.count()
    if not dirty_count:
        return
    logger.debug(f'{dirty_count} documents are to be synchronized')

    started = datetime.datetime.now()
    processed = 0
    for doc in dirty_documents:
        cache_document_fields(
            logger, doc, cache_generic_fields=True, cache_user_fields=True)
        processed += 1
        elapsed = (datetime.datetime.now() - started).total_seconds()
        if elapsed > task_limit_seconds:
            logger.debug(f'Cached {processed} documents only of {dirty_count}')
            break


class TrainAndTest(ExtendedTask):
    name = 'Train And Test'
    soft_time_limit = 6000
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3

    def process(self, **kwargs):
        self.log_info(
            'Going to train document field based on the datasets stored in DB...')

        document_field_id = kwargs.get('document_field_id')
        skip_training = kwargs.get('skip_training')
        use_only_confirmed_field_values_for_training = kwargs.get('use_only_confirmed_field_values_for_training')
        train_data_project_ids = kwargs.get('train_data_project_ids')
        split_and_log_out_of_sample_test_report = kwargs.get('split_and_log_out_of_sample_test_report')

        skip_testing = kwargs.get('skip_testing')
        use_only_confirmed_field_values_for_testing = kwargs.get('use_only_confirmed_field_values_for_testing')
        test_data_projects_ids = kwargs.get('test_data_projects_ids')

        field = DocumentField.objects.get(pk=document_field_id)

        if not field.is_detectable():
            self.log_info('Field {0} is not detectable. Nothing to train and/or test.'.format(field.code))

        new_model = None

        if not skip_training:
            if train_data_project_ids:
                self.log_info('Training model on the specified projects...')
            else:
                self.log_info('No training projects specified. '
                              'Training model on all user-confirmed field values in the system...')

            new_model = field_detection \
                .train_document_field_detector_model(CeleryTaskLogger(self),
                                                     field,
                                                     train_data_project_ids,
                                                     use_only_confirmed_field_values_for_training,
                                                     split_and_log_out_of_sample_test_report)
            if new_model:
                ClassifierModel.objects.filter(document_field=field).delete()
                new_model.save()

                if new_model.classifier_accuracy_report_in_sample:
                    self.log_info('Sklearn test report for in-sample docs:\n{0}'
                                  .format(new_model.classifier_accuracy_report_in_sample))

                if new_model.classifier_accuracy_report_out_of_sample:
                    self.log_info('Sklearn test report for out-of-sample docs:\n{0}'
                                  .format(new_model.classifier_accuracy_report_out_of_sample))
            else:
                self.log_info('No model trained. '
                              'Probably the detection strategy of field {0} does not allow training'.format(field.code))

        if skip_testing:
            return

        if not test_data_projects_ids:
            self.log_info('No test projects specified. Skiping the testing step.')
            return
        else:
            if not use_only_confirmed_field_values_for_testing:
                test_document_ids = Document.objects \
                    .filter(project_id__in=test_data_projects_ids, document_type_id=field.document_type.pk) \
                    .values_list('pk', flat=True)
            else:
                fd_repo = FieldDetectionRepository()
                test_document_ids = set(fd_repo.get_qs_active_modified_document_ids(field,
                                                                                    test_data_projects_ids))
                test_document_ids.update(set(fd_repo.get_qs_finished_document_ids(field.document_type,
                                                                                  test_data_projects_ids)))

            self.log_info('Testing field detection document-by-document...')
            test_tasks_args = []
            for test_document_id in test_document_ids:
                test_tasks_args.append((field.uid, test_document_id))

            if test_tasks_args:
                self.run_sub_tasks('Test Field Detector Model', TrainAndTest.test_field_detector_model,
                                   test_tasks_args)
                args_list = [(field.uid, new_model.pk if new_model else None)]
                self.run_after_sub_tasks_finished('Join Field Detector Model Tests',
                                                  TrainAndTest.join_field_detector_model_tests,
                                                  args_list)

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=6000,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
                 max_retries=3)
    def test_field_detector_model(task: ExtendedTask, field_id, document_id) -> dict:
        document = Document.objects.get(pk=document_id)  # type: Document
        doc_text = document.text
        field = DocumentField.objects.get(pk=field_id)  # type: DocumentField
        typed_field = TypedField.by(field)

        expected_field_value_dto = field_detection.detect_field_value(
            CeleryTaskLogger(task), document, field)  # type: FieldValueDTO

        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()

        if typed_field.requires_value:
            # dates, numbers, e.t.c.
            actual_field_value_dict = field_repo \
                .get_field_code_to_json_value(doc_id=document_id, field_codes_only={field.code})

            actual_field_value = actual_field_value_dict.get(field.code) if actual_field_value_dict else None
            expected_field_value = expected_field_value_dto.field_value if expected_field_value_dto else None

            matches = bool(expected_field_value == actual_field_value)
            status_str = '[  OK  ]' if matches else '[ ERR  ]'
            task.log_info(f'{status_str} Test doc: {document.name} '
                          f'(Doc id: {document.pk}, Project: {document.project.name or "---"}). '
                          f'Detected: {expected_field_value_dto}. Real: {actual_field_value}.')
        else:
            expected_set = set()
            expected_set_text: List[str] = list()
            # related-info e.t.c. - comparing by annotations - exact comparing
            if expected_field_value_dto and expected_field_value_dto.annotations:
                for ant_dto in expected_field_value_dto.annotations:
                    text_unit_id = field_repo.find_text_unit_id_by_location(
                        document,
                        field,
                        ant_dto.location_in_doc_start,
                        ant_dto.location_in_doc_end)
                    if not text_unit_id:
                        continue
                    expected_set.add('text_unit_' + str(text_unit_id))
                    expected_set_text.append(doc_text[ant_dto.location_in_doc_start:ant_dto.location_in_doc_end])

            actual_dfvs: List[FieldAnnotation] = FieldAnnotation.objects.filter(
                document_id=document_id,
                field_id=field.pk)

            actual_set = set()
            actual_text: List[str] = list()
            for dfv in actual_dfvs:
                if not dfv.text_unit:
                    continue
                actual_set.add('text_unit_' + str(dfv.text_unit.id))
                actual_text.append(dfv.location_text)

            actual_field_value = '; '.join(sorted(actual_set))
            matches = bool(expected_set == actual_set)

            if not matches:
                status_str = '[  OK  ]' if matches else '[ ERR  ]'
                task.log_info(f'{status_str} Test doc: {document.name} '
                              + f'(Doc id: {document.pk}, Project: {document.project.name or "---"}). '
                              + '\nDetected annotations:\n' + '\n'.join(expected_set_text)
                              + '\nReal annotations:\n' + '\n'.join(actual_text))

        text_units_number = TextUnit.objects.filter(document=document, unit_type=field.text_unit_type).count()

        return {
            'text_units_number': text_units_number,
            'value_matches_expected': matches,
            'actual_field_value': actual_field_value if typed_field.is_choice_field else None
        }

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=600,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
                 max_retries=3)
    def join_field_detector_model_tests(task: ExtendedTask,
                                        field_uid,
                                        classifier_model_id):
        results = list(Task.objects
                       .filter(main_task_id=task.main_task_id,
                               name=TrainAndTest.test_field_detector_model.name)
                       .values_list('result', flat=True))

        test_text_units_number = 0
        match_number = 0
        test_doc_number = 0

        matches_per_value = dict()
        total_per_value = dict()

        for res in results:
            actual_field_value = res.get('actual_field_value')

            if actual_field_value:
                if actual_field_value not in total_per_value:
                    total_per_value[actual_field_value] = 0
                total_per_value[actual_field_value] += 1

            test_doc_number += 1
            test_text_units_number += (res.get('text_units_number') or 0)
            if res.get('value_matches_expected'):
                match_number += 1
                if actual_field_value:
                    if actual_field_value not in matches_per_value:
                        matches_per_value[actual_field_value] = 0
                    matches_per_value[actual_field_value] += 1

        accuracy = match_number / test_doc_number

        if classifier_model_id:
            classifier_model = ClassifierModel.objects.get(pk=classifier_model_id)
            classifier_model.field_detection_accuracy = accuracy
            classifier_model.save()

        field = DocumentField.objects.get(pk=field_uid)

        task.log_info('Testing finished.\n'
                      'Document type: {0}.\n'
                      'Field: {1}.\n'
                      'Text unit type: {2}.\n'
                      'Test documents number: {3}.\n'
                      'Test text units number: {4}.\n'
                      'Accuracy: {5}.\n'
                      .format(field.document_type.code,
                              field.code,
                              field.text_unit_type,
                              test_doc_number,
                              test_text_units_number,
                              accuracy))

        if TypedField.by(field).is_choice_field:
            accuracy_per_value = {actual_field_value: (matches_per_value.get(actual_field_value) or 0) / total
                                  for actual_field_value, total in total_per_value.items()}
            task.log_info('Accuracy per value:\n{0}'.format(json.dumps(accuracy_per_value, sort_keys=True, indent=2)))


class LoadDocumentWithFields(ExtendedTask):
    name = 'Load Document With Fields'
    soft_time_limit = 6000
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3
    priority = 7

    @staticmethod
    def load_field_values(task: ExtendedTask,
                          document: Document,
                          document_fields_alias_to_value: Dict[str, Any],
                          ignore_parsing_errors: bool = False) \
            -> Dict[DocumentField, FieldValueDTO]:
        document_type = document.document_type
        fields_to_values = dict()  # type: Dict[DocumentField, FieldValueDTO]

        if not document_type:
            return fields_to_values

        field_code_aliases = document_type.field_code_aliases

        field_codes_to_fields = {f.code.lower(): f for f in document_type.fields.all()}

        if field_code_aliases:
            field_codes_to_fields.update({field_alias.lower(): field_codes_to_fields.get(field_code.lower())
                                          for field_alias, field_code in field_code_aliases.items() if
                                          field_alias and field_code})

        for field_alias, field_value_text in document_fields_alias_to_value.items():
            if field_value_text is None:
                continue

            field = field_codes_to_fields.get(field_alias.lower())  # type: DocumentField
            if not field:
                task.log_warn(f'Field alias "{field_alias}" not found for document type {document_type.code}')
                continue
            typed_field = TypedField.by(field)  # type: TypedField

            try:
                if type(field_value_text) is list:
                    for possible_value_text in list(field_value_text):
                        maybe_value = typed_field.extract_from_possible_value_text(possible_value_text)
                        if maybe_value:
                            maybe_value = typed_field.field_value_python_to_json(maybe_value)
                            fields_to_values[field] = FieldValueDTO(field_value=maybe_value)
                            break
                else:
                    maybe_value = typed_field.extract_from_possible_value_text(field_value_text)
                    if maybe_value:
                        maybe_value = typed_field.field_value_python_to_json(maybe_value)
                        fields_to_values[field] = FieldValueDTO(field_value=maybe_value)
            except Exception as e:
                msg = f'Unable to cast value "{field_value_text}" to the target type of "{field.code}"'
                if ignore_parsing_errors:
                    task.log_warn(msg)
                else:
                    raise RuntimeError(msg) from e

        return fields_to_values

    def process(self, **kwargs):
        self.log_info('Going to load document with fields...')

        document_name = kwargs.get('document_name')
        project = Project.objects.get(pk=kwargs.get('project_id'))  # type: Project
        run_detect_field_values = bool(kwargs.get('run_detect_field_values'))

        document_fields = kwargs.get('document_fields') or {}  # type: Dict

        file_storage = get_file_storage()

        if document_fields:
            document = Document(
                name=document_name,
                project=project,
                document_type=project.type,
            )
            LoadDocumentWithFields.load_doc(self, document, document_fields, run_detect_field_values)

        path = kwargs['source_data']

        if path:
            self.log_info('Parse {0} at {1}'.format(path, file_storage))
            file_list = file_storage.list_documents(path)

            self.log_info("Detected {0} files. Added {0} subtasks.".format(len(file_list)))

            if len(file_list) == 0:
                raise RuntimeError('Wrong file or directory name or directory is empty: {}'
                                   .format(path))
            load_docs_args = [(file_path, project.pk, run_detect_field_values)
                              for file_path in file_list]
            self.run_sub_tasks('Load Each Document',
                               LoadDocumentWithFields.create_document,
                               load_docs_args,
                               file_list)

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=3600,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError),
                 max_retries=3)
    def create_document(task: ExtendedTask, uri: str, project_id, run_detect_field_values):
        file_storage = get_file_storage()
        with file_storage.get_document_as_local_fn(uri) as (fn, file_name):
            task.task.title = 'Load Document: {0}'.format(uri)
            task.log_extra = {Document.LOG_FIELD_DOC_NAME: uri}

            with open(fn, encoding='utf-8') as data_file:
                data = json.loads(data_file.read())
                project = Project.objects.get(pk=project_id)
                document_type = project.type
                document = Document(
                    name=file_name,
                    project=project,
                    document_type=document_type,
                )
                LoadDocumentWithFields.load_doc(task=task,
                                                document=document,
                                                field_values_alias_to_value=data,
                                                run_detect_field_values=run_detect_field_values)

    @staticmethod
    def load_doc(task: ExtendedTask,
                 document: Document,
                 field_values_alias_to_value: Dict[str, Any],
                 run_detect_field_values: bool,
                 field_owners: Dict[str, User] = None,
                 skip_modified_values: bool = False,
                 field_codes_to_detect: Optional[List[str]] = None,
                 clear_old_values=False,
                 ignore_parsing_errors=False):
        field_owners = field_owners if field_owners else {}
        fields_to_values = LoadDocumentWithFields.load_field_values(
            task, document, field_values_alias_to_value, ignore_parsing_errors)
        log = CeleryTaskLogger(task)
        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()

        with transaction.atomic():
            new_document = document.pk is None
            document.save(force_insert=new_document)
            # document metadata might have already been created
            if not DocumentMetadata.objects.filter(document_id=document.pk).exists():
                DocumentMetadata.objects.create(document=document, metadata={'parsed_by': None})

            for field, value_dto in fields_to_values.items():
                field_repo.update_field_value_with_dto(document=document,
                                                       field=field,
                                                       field_value_dto=value_dto,
                                                       user=field_owners.get(field.code))

            if run_detect_field_values:
                field_detection.detect_and_cache_field_values_for_document(
                    log=log,
                    document=document,
                    save=True,
                    clear_old_values=clear_old_values,
                    skip_modified_values=skip_modified_values,
                    field_codes_to_detect=field_codes_to_detect)
            else:
                signals.fire_document_changed(sender=task,
                                              log=log,
                                              document=document,
                                              changed_by_user=None,
                                              document_initial_load=True,
                                              system_fields_changed=True,
                                              generic_fields_changed=True,
                                              user_fields_changed=True)

        field_values_text = ''
        for f, dto in fields_to_values.items():
            fval_str = str(dto.field_value)
            fval_str = fval_str[:128] + '...' if fval_str and len(fval_str) > 128 else ''
            field_values_text += f'{f}: {fval_str}\n'

        task.log_info(f'Loaded {len(fields_to_values)} field values for document '
                      f'#{document.pk} ({document.name}): {field_values_text}')


class ImportCSVFieldDetectionConfig(ExtendedTask):
    name = 'Import CSV Field Detection Config'
    soft_time_limit = 6000
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3

    def process(self,
                document_field: Dict,
                config_csv_file: Dict,
                drop_previous_field_detectors: bool,
                update_field_choice_values: bool,
                csv_contains_regexps: bool,
                selected_columns: str = None,
                wrap_in_wordbreaks: bool = True,
                save_in_csv_format: bool = True,
                **kwargs):
        try:
            self.log_info('Going to configure simple field detection config...')
            document_field = DocumentField.objects.get(pk=document_field['pk'])
            csv_bytes = DbCache.get(config_csv_file['cache_key'])
            importer = CsvDetectorImporter(
                CeleryTaskLogger(self),
                document_field,
                drop_previous_field_detectors,
                update_field_choice_values,
                csv_contains_regexps=csv_contains_regexps,
                selected_columns=selected_columns,
                wrap_in_wordbreaks=wrap_in_wordbreaks,
                save_in_csv_format=save_in_csv_format)
            importer.process_csv(csv_bytes)
        finally:
            DbCache.clean_cache(config_csv_file['cache_key'])


class FindBrokenDocumentFieldValues(ExtendedTask):
    name = 'Find Broken Document Field Values'

    @staticmethod
    def process_broken(task: ExtendedTask, obj: Union[FieldAnnotation, FieldValue], delete_broken: bool = False):
        if delete_broken:
            obj.delete()
            task.log_info(f'Found broken {obj.__class__.__name__}.\n'
                          f'Id: {obj.pk}\n'
                          f'JSON value: {obj.value}\n'
                          'The document field value has been deleted.\n')
        else:
            content_type = ContentType.objects.get_for_model(obj.__class__)
            dfv_admin_url = reverse("admin:%s_%s_change" %
                                    (content_type.app_label, content_type.model), args=(obj.pk,))
            task.log_info(f'Found broken {obj.__class__.__name__}.\n'
                          f'Id: {obj.pk}\n'
                          f'JSON value: {obj.value}\n'
                          f'Admin URL: {dfv_admin_url}\n')

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=3600,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError),
                 max_retries=3)
    def check_annotations(task: ExtendedTask,
                          annotation_ids: List[int],
                          delete_broken: bool = False):
        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()
        qa_ants = field_repo.get_ants_by_ids(annotation_ids)
        for ant in qa_ants:  # FieldAnnotation
            field = ant.field  # type: DocumentField
            if not TypedField.by(field).is_json_annotation_value_ok(ant.value):
                FindBrokenDocumentFieldValues.process_broken(task, ant, delete_broken)

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=3600,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError),
                 max_retries=3)
    def check_field_values(task: ExtendedTask,
                           field_value_ids: List[int],
                           delete_broken: bool = False):
        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()
        qa_field_values = field_repo.get_field_values_by_ids(field_value_ids)
        for fv in qa_field_values:  # FieldAnnotation
            field = fv.field  # type: DocumentField
            if not TypedField.by(field).is_json_field_value_ok(fv.value):
                FindBrokenDocumentFieldValues.process_broken(task, fv, delete_broken)

    def process(self, **kwargs):
        document_field_arg = kwargs.get('document_field')
        document_field_id = document_field_arg['pk'] if document_field_arg else None

        delete_broken = kwargs.get('delete_broken')

        # check FieldValue-s
        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()

        qs_field_values = field_repo.get_field_value_ids_by_doc_field(document_field_id)
        total_num = qs_field_values.count()
        for ids_chunk in chunks(qs_field_values.values_list('pk', flat=True), 100):
            id_list = list(ids_chunk)
            self.run_sub_tasks('Check FieldValues',
                               self.check_field_values,
                               [(id_list, delete_broken)])
            self.log_info(f'Sub-tasks started for {len(id_list)} FieldValues of total {total_num}')

        # check FieldAnnotation-s
        qs_ants = field_repo.get_fieldant_ids_by_doc_field(document_field_id)
        total_num = qs_ants.count()
        for ids_chunk in chunks(qs_ants.values_list('pk', flat=True), 100):
            id_list = list(ids_chunk)
            self.run_sub_tasks('Check FieldAnnotations',
                               self.check_annotations,
                               [(id_list, delete_broken)])
            self.log_info(f'Sub-tasks started for {len(id_list)} FieldAnnotations of total {total_num}')


class ImportDocumentType(ExtendedTask):
    name = 'Import Document Type'
    soft_time_limit = 3600
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3

    def process(self,
                document_type_config_json_file: Dict,
                action: str,
                update_cache: bool,
                source_version: Optional[str],
                **kwargs):

        if action == 'validate':
            save = False
            auto_fix_validation_errors = False
            remove_missed_objects = False
        elif action == 'validate|import':
            save = True
            auto_fix_validation_errors = False
            remove_missed_objects = False
        elif action == 'import|auto_fix|retain_missing_objects':
            save = True
            auto_fix_validation_errors = True
            remove_missed_objects = False
        elif action == 'import|auto_fix|remove_missing_objects':
            save = True
            auto_fix_validation_errors = True
            remove_missed_objects = True
        else:
            raise RuntimeError('Unknown action')

        source_version = int(source_version) if source_version else CURRENT_VERSION

        try:
            json_bytes = DbCache.get(document_type_config_json_file['cache_key'])
            document_type = import_document_type(json_bytes=json_bytes,
                                                 save=save,
                                                 auto_fix_validation_errors=auto_fix_validation_errors,
                                                 remove_missed_in_dump_objects=remove_missed_objects,
                                                 source_version=source_version,
                                                 task=self)
        finally:
            DbCache.clean_cache(document_type_config_json_file['cache_key'])

        if not (save and update_cache):
            return

        from apps.rawdb.app_vars import APP_VAR_DISABLE_RAW_DB_CACHING
        if not APP_VAR_DISABLE_RAW_DB_CACHING.val:
            self.log_info('Adapting RawDB table structure after import ...')
            adapt_table_structure(CeleryTaskLogger(self), document_type, force=False)
        ids = Document.all_objects.filter(document_type=document_type).values_list('pk', flat=True)
        ids = list(ids)
        self.log_info('Caching document field values ...')

        for chunk in chunks(ids, 50):
            self.run_sub_tasks('Cache field values for a set of documents',
                               ImportDocumentType.cache_document_fields_for_doc_ids,
                               [(list(chunk),)])

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=6000,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError),
                 max_retries=0)
    def cache_document_fields_for_doc_ids(_task: ExtendedTask, doc_ids: List):
        for doc in Document.all_objects.filter(pk__in=doc_ids):
            log = CeleryTaskLogger(_task)
            detect_and_cache_field_values_for_document(
                log, doc, False, clear_old_values=False)


class ExportDocuments(ExtendedTask):
    name = 'Export Documents'
    soft_time_limit = 3600
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3

    DOC_PER_PACK = 10

    def process(self,
                document_ids: List[int],
                project_ids: List[int],
                document_type_id: str,
                file_path: str,
                export_files: bool,
                **_kwargs):
        storage = get_file_storage()
        document_ids.sort()
        temp_dir = tempfile.mkdtemp()
        mem_stream = io.BytesIO()
        try:
            for ids_pack in list(chunks(document_ids, self.DOC_PER_PACK)):
                exporter = DocumentExporter()
                first_id, last_id = min(ids_pack), max(ids_pack)
                self.log_info(f'Exporting chunk {first_id}:{last_id}')
                part_name = os.path.join(temp_dir, f'part_{first_id}_{last_id}.zip')

                subfolder = os.path.join(temp_dir, str(first_id))
                os.mkdir(subfolder)
                zipped_stream = exporter.in_memory_export(
                    ids_pack, subfolder, export_files)
                self.log_info(f'Chunk {first_id}:{last_id} is in memory')

                with open(part_name, 'wb') as fw:
                    buffer = zipped_stream.getbuffer()
                    fw.write(buffer)
                self.log_info(f'Chunk {first_id}:{last_id} is on disk')

            self.log_info(f'Saving all chunks in one file')
            with zipfile.ZipFile(mem_stream, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for name_only in os.listdir(temp_dir):
                    fn = os.path.join(temp_dir, name_only)
                    if not os.path.isfile(fn):
                        continue
                    zip_file.write(fn, arcname=name_only)
            self.log_info('Saving all chunks: completed')
        finally:
            rmtree(temp_dir)

        buffer_bytes = mem_stream.getbuffer()
        storage.write_file(file_path, buffer_bytes, buffer_bytes.nbytes)
        self.log_info('File is saved to the storage')
        # log path
        self.log_info(f'ExportDocuments - path is "{file_path}"')
        try:
            file_ref = ExportFile.objects.get(file_path=file_path)  # type: ExportFile
            self.log_info(f'ExportDocuments - absolute path is "{file_ref.get_link(abs_path=True)}"')
            file_ref.file_created = True
            file_ref.stored_time = datetime.datetime.utcnow()
            file_ref.save()
            if project_ids:
                sub_msg = 'project(s) {}'.format(
                    ', '.join(Project.objects.filter(id__in=project_ids)
                              .values_list('name', flat=True)))
            elif document_type_id:
                sub_msg = 'document type "{}"'.format(
                    DocumentType.objects.get(pk=document_type_id).title)
            else:
                sub_msg = '{} document(s)'.format(len(document_ids))
            file_ref.send_email(log=CeleryTaskLogger(self),
                                subject=f'Document Files from {sub_msg} Ready to Download')
            self.log_info(f'Email sent to user {file_ref.user.get_full_name()}')
        except Exception as e:
            self.log_error(f'Error updating "ExportFile" reference: {e}, path: "{file_path}"')
            raise


class ImportDocuments(ExtendedTask):
    name = 'Import Documents'
    soft_time_limit = 12 * 3600
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 2

    def process(self,
                document_import_file: Dict,
                project: Optional[Dict[str, Any]],
                user_id: Optional[int],
                import_files: Optional[bool],
                **_kwargs):
        with download_task_attached_file(document_import_file) as fn:
            # download file parts (if any) and create a subtask per each part
            with zipfile.ZipFile(fn, 'r') as zip_ref:
                file_names = zip_ref.filelist
                names_only = [f.filename for f in file_names]
                if 'project_project.zip' in names_only:
                    self.log_info('Importing all documents in one task')
                    # import in the current task as one package
                    self.import_documents_package(document_import_file,
                                                  project, user_id, import_files)
                    return
                # extract sub-archives and start a task for each one
                self.log_info(f'Spawning {len(file_names)} subtasks')
                sub_args = []
                for sub_archive in file_names:
                    file_bytes = zip_ref.read(sub_archive)
                    cache_key = f'{datetime.datetime.now()}_{sub_archive.filename}'
                    DbCache.put_to_db(cache_key, file_bytes)

                    sub_args.append({'document_import_file': {'cache_key': cache_key,
                                                              'file_name': sub_archive.filename},
                                     'project': project,
                                     'user_id': user_id,
                                     'import_files': import_files})

                self.run_sub_tasks_class_based(
                    'Import Documents Pack',
                    ImportDocumentsPack,
                    sub_args)

    def import_documents_package(self,
                                 document_import_file: Dict,
                                 project: Optional[Dict[str, Any]],
                                 user_id: Optional[int],
                                 import_files: Optional[bool]):
        importer = DocumentImporter(self)
        task_user = self.task.user_id
        target_user = user_id or task_user
        project_obj = Project.objects.get(pk=project['pk']) if project else None
        with download_task_attached_file(document_import_file) as fn:
            importer.import_documents(fn, project_obj, target_user, import_files or False)

        doc_id_status = [(id, True,) for id in importer.initially_loaded_docs]
        doc_id_status += [(id, False,) for id in importer.updated_docs]
        self.log_info(f'{len(importer.initially_loaded_docs)} docs are loaded, ' +
                      f'{len(importer.updated_docs)} docs are updated')

        self.log_info(f'Locating values for {len(doc_id_status)} documents')
        for doc_id, init_loaded in doc_id_status:
            from apps.extract.app_vars import STANDARD_LOCATORS
            self.run_sub_tasks_class_based('Locate', Locate, [{
                'locate': list(set(STANDARD_LOCATORS.val + ['term'])),
                'parse': ['sentence'],
                'do_delete': False,
                'user_id': task_user,
                'document_id': doc_id,
                'doc_loaded_by_user_id': task_user,
                'document_initial_load': init_loaded,
                'detect_field_values': False,
                'fire_doc_changed': False,
                'cache_field_values': True
            }])


class ImportDocumentsPack(ImportDocuments):
    name = 'Import Documents Pack'
    soft_time_limit = 6 * 3600
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 2

    def process(self,
                document_import_file: Dict,
                project: Optional[Dict[str, Any]],
                user_id: Optional[int],
                import_files: Optional[bool],
                **_kwargs):
        self.import_documents_package(document_import_file,
                                      project, user_id, import_files)


class FixDocumentFieldCodes(ExtendedTask):
    name = 'Fix Document Field Codes'
    priority = 7

    RE_FIELD_CODE_NUM = re.compile(r'(.*)_(\d+)')

    def process(self, **kwargs):

        with transaction.atomic():

            csv_log = list()  # type: List[Tuple[str, str, str]]
            for document_type in DocumentType.objects.all():  # type: DocumentType
                changed_field_codes = dict()  # type: Dict[str, str]
                field_code_use_counts = dict()  # type: Dict[str, int]

                for code in DocumentField.objects \
                        .filter(document_type=document_type) \
                        .order_by('order', 'code') \
                        .values_list('code', flat=True):
                    field_code_use_counts[code] = 1
                    m = self.RE_FIELD_CODE_NUM.fullmatch(code)
                    if m:
                        base = m.group(1)
                        num = int(m.group(2))
                        old_num = field_code_use_counts.get(base) or 0
                        field_code_use_counts[base] = max(old_num, num)

                for field in DocumentField.objects \
                        .filter(document_type=document_type) \
                        .order_by('order', 'code'):  # type: DocumentField
                    field_code_escaped = escape_column_name(field.code)[:DOCUMENT_FIELD_CODE_MAX_LEN]

                    if field.code == field_code_escaped:
                        field_code_use_counts[field.code] = (field_code_use_counts.get(field.code) or 0) + 1
                        long_code = DocumentField.get_long_code(field, document_type)
                        if field.long_code != long_code:
                            self.log_info('Updating field long code {0} to {1}'
                                          .format(field.long_code, long_code))
                            field.long_code = long_code
                            field.save(update_fields={'long_code'})
                    else:
                        field_code_use_count = field_code_use_counts.get(field_code_escaped)
                        if field_code_use_count is not None:
                            field_code_use_counts[field_code_escaped] = field_code_use_count + 1
                            counter_str = str(field_code_use_count)

                            # make next repeated column name to be column1, column2, ...
                            # make it fitting into N chars by cutting the field code on the required
                            # number of chars to fit the num
                            field_code_escaped = field_code_escaped[:DOCUMENT_FIELD_CODE_MAX_LEN - len(counter_str) - 1] \
                                                 + '_' + counter_str
                        else:
                            field_code_use_counts[field_code_escaped] \
                                = (field_code_use_counts.get(field_code_escaped) or 0) + 1

                        self.log_info('Updating field {0}.{1} to {2}'
                                      .format(document_type.code, field.code, field_code_escaped))
                        changed_field_codes[field.code] = field_code_escaped
                        csv_log.append((document_type.code, field.code, field_code_escaped))
                        field.code = field_code_escaped

                        field.long_code = DocumentField.get_long_code(field, document_type)

                        field.save(update_fields={'code', 'long_code'})

                    hide_until_js = jiphy.to.javascript(field.hide_until_python) if field.hide_until_python else ''
                    if hide_until_js != field.hide_until_js:
                        field.hide_until_js = hide_until_js
                        self.log_info('Updating hide_until_js for field {0}.{1}'
                                      .format(document_type.code, field.code))
                        field.save(update_fields={'hide_until_js'})

                if len(changed_field_codes) > 0 and document_type.field_code_aliases:
                    updated_aliases = {k: changed_field_codes.get(v) or v
                                       for k, v in document_type.field_code_aliases.items()}
                    self.log_info('Updating field code aliases of document type {0}"\n{1}'
                                  .format(document_type.code, updated_aliases))
                    document_type.field_code_aliases = updated_aliases
                    document_type.save(update_fields={'field_code_aliases'})

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(('Document Type', 'Old Field Code', 'New Field Code'))
        for r in csv_log:
            writer.writerow(r)
        self.log_info('\n\n\n------------------\n'
                      'Changed fields csv:\n' + output.getvalue() + '\n------------------')


class DeleteDocuments(ExtendedTask):
    name = 'Delete Documents'
    priority = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.document.repository.document_repository import default_document_repository
        self.document_repository = default_document_repository

    def process(self, **kwargs):
        doc_ids = kwargs.get('_document_ids')
        safe_mode = kwargs.get('_safe_mode')
        safe_mode = safe_mode if safe_mode is not None else True
        file_paths = self.document_repository.get_all_document_source_paths(doc_ids)
        get_document_bulk_delete(safe_mode).delete_documents(doc_ids)
        DocumentFilesCleaner.delete_document_files(file_paths, self.log_error)


@shared_task(base=ExtendedTask,
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def _process_documents_status_changed(task: ExtendedTask, doc_ids: List, new_status_id: int, changed_by_user_id: int):
    from apps.document.repository.document_field_repository import DocumentFieldRepository

    dfr = DocumentFieldRepository()

    status = ReviewStatus.objects.get(pk=new_status_id)  # type: ReviewStatus
    docs_qr = Document.objects.filter(pk__in=doc_ids)
    changed_by_user = User.objects.get(pk=changed_by_user_id) if changed_by_user_id is not None else None
    if not status.is_active:
        for doc in docs_qr:
            dfr.delete_hidden_field_values_if_needed(doc, event_sender=task)

    fire_documents_status_changed(sender=task,
                                  documents=docs_qr,
                                  new_status_id=new_status_id,
                                  changed_by_user=changed_by_user)


@shared_task(base=ExtendedTask,
             name='Documents Status Changed',
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def process_documents_status_changed(task: ExtendedTask, doc_ids: List, new_status_id: int, changed_by_user_id: int):
    task.run_sub_tasks('Process doc status change',
                       _process_documents_status_changed,
                       [(l, new_status_id, changed_by_user_id) for l in chunks(doc_ids, DOC_NUMBER_PER_SUB_TASK)])


@shared_task(base=ExtendedTask,
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def _process_documents_assignee_changed(task: ExtendedTask,
                                        doc_ids: List[int],
                                        new_assignee_id: int,
                                        changed_by_user_id: int):
    changed_by_user = User.objects.get(pk=changed_by_user_id) if changed_by_user_id is not None else None
    fire_documents_assignee_changed(sender=task, doc_ids=doc_ids, new_assignee_id=new_assignee_id,
                                    changed_by_user=changed_by_user)


@shared_task(base=ExtendedTask,
             name='Documents Assignee Changed',
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def process_documents_assignee_changed(task: ExtendedTask, doc_ids: List, new_assignee_id: int,
                                       changed_by_user_id: int):
    task.run_sub_tasks('Process doc assignee change',
                       _process_documents_assignee_changed,
                       [(l, new_assignee_id, changed_by_user_id) for l in chunks(doc_ids, DOC_NUMBER_PER_SUB_TASK)])


@shared_task(base=ExtendedTask,
             name='Document Changed',
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def process_document_changed(task: ExtendedTask,
                             doc_id: int,
                             system_fields_changed: FieldSpec = True,
                             generic_fields_changed: FieldSpec = True,
                             user_fields_changed: bool = True,
                             changed_by_user_id: int = None):
    from apps.document.repository.document_field_repository import DocumentFieldRepository

    dfr = DocumentFieldRepository()

    doc = Document.objects.get(pk=doc_id)  # type: Document
    changed_by_user = User.objects.get(pk=changed_by_user_id) if changed_by_user_id is not None else None
    if DocumentSystemField.status.specified_in(system_fields_changed):
        dfr.delete_hidden_field_values_if_needed(doc, event_sender=task)
    fire_document_changed(sender=task,
                          log=CeleryTaskLogger(task),
                          document=doc,
                          changed_by_user=changed_by_user,
                          document_initial_load=False,
                          system_fields_changed=system_fields_changed,
                          generic_fields_changed=generic_fields_changed,
                          user_fields_changed=user_fields_changed)


def plan_process_document_changed(doc_id: int,
                                  system_fields_changed: FieldSpec = True,
                                  generic_fields_changed: FieldSpec = True,
                                  user_fields_changed: bool = True,
                                  changed_by_user_id: int = None):
    call_task_func(process_document_changed,
                   (doc_id, system_fields_changed, generic_fields_changed, user_fields_changed, changed_by_user_id),
                   changed_by_user_id)


def plan_process_documents_status_changed(doc_ids: Iterable, new_status_id: int, changed_by_user_id: int):
    """
    Plans processing of the documents status change. Starts multiple tasks, N doc ids per task, to avoid
    possible overloading the rabbitmq if too large set of doc ids is provided.
    Each started task will be shown in the admin task list and may start any number of sub-tasks to parallelize
    the processing.
    :param doc_ids:
    :param new_status_id:
    :param changed_by_user_id:
    :return:
    """
    for doc_ids_chunk in chunks(doc_ids, DOC_NUMBER_PER_MAIN_TASK):
        call_task_func(process_documents_status_changed,
                       (doc_ids_chunk, new_status_id, changed_by_user_id),
                       changed_by_user_id)


def plan_process_documents_assignee_changed(doc_ids: Iterable,
                                            new_assignee_id: Optional[int] = None,
                                            changed_by_user_id: Optional[int] = None):
    """
    Plans processing of the documents assignee change. Starts multiple tasks, N doc ids per task, to avoid
    possible overloading the rabbitmq if too large set of doc ids is provided.
    Each started task will be shown in the admin task list and may start any number of sub-tasks to parallelize
    the processing.
    :param doc_ids:
    :param new_assignee_id:
    :param changed_by_user_id:
    :return:
    """
    for doc_ids_chunk in chunks(doc_ids, DOC_NUMBER_PER_MAIN_TASK):
        call_task_func(process_documents_assignee_changed,
                       (doc_ids_chunk, new_assignee_id, changed_by_user_id),
                       changed_by_user_id)


@shared_task(base=ExtendedTask,
             name=task_names.TASK_NAME_IDENTIFY_CONTRACTS,
             bind=True,
             soft_time_limit=6000,
             default_retry_delay=10,
             retry_backoff=True,
             autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
             max_retries=3)
def identify_contracts(task: ExtendedTask,
                       document_type_code: Optional[str] = None,
                       force: Optional[bool] = False,
                       project_id: Optional[int] = None):
    run_parameters = {'document type': document_type_code}
    if project_id:
        run_parameters['project'] = project_id
    if force:
        run_parameters['force'] = True
    ptrs_str = ', '.join([f'{p}={run_parameters[p]}' for p in run_parameters])

    msg = f'identify_contracts called for {ptrs_str}. ' \
        f'Task: {task.task_name}, main id: {task.main_task_id}'
    log = CeleryTaskLogger(task)
    log.info(msg)
    identify_document_classes(task, document_type_code, force, project_id)


def identify_document_classes(task: ExtendedTask,
                              document_type_code: str = None,
                              force_recheck: bool = False,
                              project_id: Optional[int] = None):
    docs = Document.objects.all()
    if not force_recheck:
        docs = docs.filter(Q(document_class='') | Q(document_class__isnull=True))
    if document_type_code:
        docs = docs.filter(document_type__code=document_type_code)
    if project_id:
        docs = docs.filter(project__id=project_id)

    total = docs.count()
    log = CeleryTaskLogger(task)
    log.info(f'identify_document_classes: {total}')
    counter = 0
    total_contracts, total_generics = (0, 0)

    for doc in docs:  # type: Document
        # doc_pk, doc_text in docs.values_list('pk', 'documenttext__full_text'):
        new_task_progress = round(counter * 100 / total)
        if new_task_progress != task.task.progress:
            task.task.progress = new_task_progress
            task.task.save()
        counter += 1

        doc_text = doc.full_text or ''
        try:
            res = is_contract(doc_text, return_probability=True)
        except Exception as e:
            log.error(f'Error in is_contract() call for document #{doc.pk}, {len(doc_text)} characters',
                      exc_info=e)
            continue
        if not res:
            log.info(f'Contract flag was not obtained for doc #{doc.pk} ({doc.name})')
            continue

        if res[0]:
            total_contracts += 1
        else:
            total_generics += 1
        meta = DocumentMetadata.objects.get(document_id=doc.pk)
        if not meta:
            meta = DocumentMetadata(document=doc, metadata={})
        doc.document_class = DocumentClass.CONTRACT if res[0] else DocumentClass.GENERIC
        meta.metadata[DOC_METADATA_DOCUMENT_CLASS_PROB] = res[1]
        meta.save()

        # caching ...
        cache_document_fields(log, doc, cache_generic_fields=False, cache_user_fields=False)

    log.info(f'{total} documents are processed ({total_contracts} contracts, {total_generics} general docs)')


class DeleteDocumentTypes(ExtendedTask):
    """
    Run Cleanup Project for all the projects, passed by ids in arguments,
    delete Document Types
    """
    name = 'Delete Document Types'

    def process(self, **kwargs):
        document_type_ids = kwargs.get('document_type_ids')

        # 1. delete related projects
        project_ids = list(Project.objects.filter(type_id__in=document_type_ids).values_list('pk', flat=True))
        clean_projects_args = [{'_project_ids': project_ids,
                                'delete': True,
                                'safe_delete': False}]

        from apps.project.tasks import CleanProjects
        self.run_sub_tasks_class_based('Clean Projects', CleanProjects, clean_projects_args)

        # 2, delete doc type itself, send signals
        self.run_after_sub_tasks_finished('Delete Document Type itself',
                                          self.delete_document_type,
                                          [(i,) for i in document_type_ids])

    @staticmethod
    @shared_task(base=ExtendedTask,
                 bind=True,
                 soft_time_limit=600,
                 default_retry_delay=10,
                 retry_backoff=True,
                 autoretry_for=(SoftTimeLimitExceeded, InterfaceError, OperationalError,),
                 max_retries=3)
    def delete_document_type(task: ExtendedTask, document_type_pk):
        dt = DocumentType.objects.get(pk=document_type_pk)
        dt.delete()
        from apps.document.signals import document_type_deleted
        document_type_deleted.send(task.__class__, user=None, document_type=dt)


class ExportDocumentFiles(ExtendedTask):
    """
    Archive document files
    """
    name = 'Export Document Files'
    soft_time_limit = 3600
    default_retry_delay = 10
    retry_backoff = True
    autoretry_for = (SoftTimeLimitExceeded, InterfaceError, OperationalError,)
    max_retries = 3
    priority = 7

    def process(self, **kwargs):
        document_ids = kwargs.get('document_ids')
        project_id = kwargs.get('project_id')
        user_id = kwargs.get('user_id')

        documents = Document.objects.filter(id__in=document_ids)
        user = User.objects.get(pk=user_id)
        project = Project.objects.get(pk=project_id)

        file_storage = get_file_storage()
        from apps.document.views import ExportDocumentsView
        file_ref = ExportDocumentsView.get_file_ref(document_ids, [project_id], user)
        mem_stream = io.BytesIO()

        with zipfile.ZipFile(mem_stream, 'w', zipfile.ZIP_DEFLATED) as zip_archive:
            for doc in documents:
                with file_storage.get_document_as_local_fn(doc.source_path) as (full_name, _):
                    try:
                        dst = os.path.join(doc.documentcluster_set.last().name, doc.name) \
                            if doc.document_type.is_generic() else doc.name
                        zip_archive.writestr(dst, open(full_name, 'rb').read())
                    except FileNotFoundError:
                        pass

        nbytes = mem_stream.tell()
        mem_stream.seek(0)
        file_storage.write_file(file_ref.file_path, mem_stream, nbytes)

        self.log_info('File is saved to the storage')
        self.log_info(f'ExportDocuments - path is "{file_ref.file_path}"')
        self.log_info(f'ExportDocuments - absolute path is "{file_ref.get_link(abs_path=True)}"')
        file_ref.file_created = True
        file_ref.stored_time = datetime.datetime.utcnow()
        file_ref.save()
        file_ref.send_email(
            log=CeleryTaskLogger(self),
            subject=f'Document Files from project "{project.name}" Ready to Download')
        self.log_info(f'Email sent to user {user.get_full_name()}')


app.register_task(DetectFieldValues())
app.register_task(TrainDocumentFieldDetectorModel())
app.register_task(TrainDirtyDocumentFieldDetectorModel())
app.register_task(TrainAndTest())
app.register_task(LoadDocumentWithFields())
app.register_task(ImportCSVFieldDetectionConfig())
app.register_task(FindBrokenDocumentFieldValues())
app.register_task(ImportDocumentType())
app.register_task(ExportDocuments())
app.register_task(ImportDocuments())
app.register_task(ImportDocumentsPack())
app.register_task(FixDocumentFieldCodes())
app.register_task(DeleteDocuments())
app.register_task(DeleteDocumentTypes())
app.register_task(ExportDocumentFiles())
