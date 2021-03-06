import copy
import json
import logging
import os
import re
import warnings
from collections import OrderedDict
from decimal import Decimal

from cove.views import cove_web_input_error, explore_data_context
from dateutil import parser
from django.conf import settings
from django.shortcuts import render
from django.utils import translation
from django.utils.html import format_html
from django.utils.translation import ugettext_lazy as _
from libcove.lib.common import get_spreadsheet_meta_data
from libcove.lib.converters import convert_json, convert_spreadsheet
from libcove.lib.exceptions import CoveInputDataError
from libcoveocds.common_checks import common_checks_ocds
from libcoveocds.config import LibCoveOCDSConfig
from libcoveocds.schema import SchemaOCDS
from strict_rfc3339 import validate_rfc3339

from cove_ocds.lib.views import group_validation_errors

from .lib import exceptions
from .lib.ocds_show_extra import add_extra_fields

logger = logging.getLogger(__name__)


@cove_web_input_error
def explore_ocds(request, pk):
    context, db_data, error = explore_data_context(request, pk)
    if error:
        return error

    lib_cove_ocds_config = LibCoveOCDSConfig()
    lib_cove_ocds_config.config["current_language"] = translation.get_language()
    lib_cove_ocds_config.config["schema_version_choices"] = settings.COVE_CONFIG[
        "schema_version_choices"
    ]
    lib_cove_ocds_config.config["schema_codelists"] = settings.COVE_CONFIG[
        "schema_codelists"
    ]

    upload_dir = db_data.upload_dir()
    upload_url = db_data.upload_url()
    file_name = db_data.original_file.file.name
    file_type = context["file_type"]

    post_version_choice = request.POST.get("version")
    replace = False
    validation_errors_path = os.path.join(upload_dir, "validation_errors-3.json")

    if file_type == "json":
        # open the data first so we can inspect for record package
        with open(file_name, encoding="utf-8") as fp:
            try:
                json_data = json.load(
                    fp, parse_float=Decimal, object_pairs_hook=OrderedDict
                )
            except UnicodeError as err:
                raise CoveInputDataError(context={
                    'sub_title': _("Sorry, we can't process that data"),
                    'link': 'index',
                    'link_text': _('Try Again'),
                    'msg': format_html(_("The file that you uploaded doesn't appear to be well formed JSON. OCDS JSON follows the I-JSON format, which requires UTF-8 encoding. Ensure that your file uses UTF-8 encoding, then try uploading again."
                             '\n\n<span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true">'
                             '</span> <strong>Error message:</strong> {}'), err),
                    'error': format(err)
                })
            except ValueError as err:
                raise CoveInputDataError(
                    context={
                        "sub_title": _("Sorry, we can't process that data"),
                        "link": "index",
                        "link_text": _("Try Again"),
                        "msg": format_html(
                            _(
                                "We think you tried to upload a JSON file, but it is not well formed JSON."
                                '\n\n<span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true">'
                                "</span> <strong>Error message:</strong> {}",
                            ),
                            err,
                        ),
                        "error": format(err),
                    }
                )

            if not isinstance(json_data, dict):
                raise CoveInputDataError(
                    context={
                        "sub_title": _("Sorry, we can't process that data"),
                        "link": "index",
                        "link_text": _("Try Again"),
                        "msg": _(
                            "OCDS JSON should have an object as the top level, the JSON you supplied does not."
                        ),
                    }
                )

            version_in_data = json_data.get("version", "")
            db_data.data_schema_version = version_in_data
            select_version = post_version_choice or db_data.schema_version
            schema_ocds = SchemaOCDS(
                select_version=select_version,
                release_data=json_data,
                lib_cove_ocds_config=lib_cove_ocds_config,
                record_pkg="records" in json_data
            )

            if schema_ocds.missing_package:
                exceptions.raise_missing_package_error()
            if schema_ocds.invalid_version_argument:
                # This shouldn't happen unless the user sends random POST data.
                exceptions.raise_invalid_version_argument(post_version_choice)
            if schema_ocds.invalid_version_data:
                if isinstance(version_in_data, str) and re.compile(
                    "^\d+\.\d+\.\d+$"
                ).match(version_in_data):
                    exceptions.raise_invalid_version_data_with_patch(version_in_data)
                else:
                    if not isinstance(version_in_data, str):
                        version_in_data = "{} (it must be a string)".format(
                            str(version_in_data)
                        )
                    context["unrecognized_version_data"] = version_in_data

            if schema_ocds.version != db_data.schema_version:
                replace = True
            if schema_ocds.extensions:
                schema_ocds.create_extended_schema_file(upload_dir, upload_url)
            url = schema_ocds.extended_schema_file or schema_ocds.schema_url

            if "records" in json_data:
                context["conversion"] = None
            else:
                # Replace the spreadsheet conversion only if it exists already.
                converted_path = os.path.join(upload_dir, "flattened")
                replace_converted = replace and os.path.exists(converted_path + ".xlsx")

                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore')  # flattentool uses UserWarning, so can't set a specific category

                    convert_json_context = convert_json(
                        upload_dir,
                        upload_url,
                        file_name,
                        lib_cove_ocds_config,
                        schema_url=url,
                        replace=replace_converted,
                        request=request,
                        flatten=request.POST.get("flatten"),
                    )

                context.update(convert_json_context)

    else:
        # Use the lowest release pkg schema version accepting 'version' field
        metatab_schema_url = SchemaOCDS(
            select_version="1.1", lib_cove_ocds_config=lib_cove_ocds_config
        ).pkg_schema_url
        metatab_data = get_spreadsheet_meta_data(
            upload_dir, file_name, metatab_schema_url, file_type
        )
        if "version" not in metatab_data:
            metatab_data["version"] = "1.0"
        else:
            db_data.data_schema_version = metatab_data["version"]

        select_version = post_version_choice or db_data.schema_version
        schema_ocds = SchemaOCDS(
            select_version=select_version,
            release_data=metatab_data,
            lib_cove_ocds_config=lib_cove_ocds_config,
        )

        # Unlike for JSON data case above, do not check for missing data package
        if schema_ocds.invalid_version_argument:
            # This shouldn't happen unless the user sends random POST data.
            exceptions.raise_invalid_version_argument(post_version_choice)
        if schema_ocds.invalid_version_data:
            version_in_data = metatab_data.get("version")
            if re.compile("^\d+\.\d+\.\d+$").match(version_in_data):
                exceptions.raise_invalid_version_data_with_patch(version_in_data)
            else:
                context["unrecognized_version_data"] = version_in_data

        # Replace json conversion when user chooses a different schema version.
        if db_data.schema_version and schema_ocds.version != db_data.schema_version:
            replace = True

        if schema_ocds.extensions:
            schema_ocds.create_extended_schema_file(upload_dir, upload_url)
        url = schema_ocds.extended_schema_file or schema_ocds.schema_url
        pkg_url = schema_ocds.pkg_schema_url

        context.update(
            convert_spreadsheet(
                upload_dir,
                upload_url,
                file_name,
                file_type,
                lib_cove_ocds_config,
                schema_url=url,
                pkg_schema_url=pkg_url,
                replace=replace,
            )
        )

        with open(context["converted_path"], encoding="utf-8") as fp:
            json_data = json.load(
                fp, parse_float=Decimal, object_pairs_hook=OrderedDict
            )

    if replace:
        if os.path.exists(validation_errors_path):
            os.remove(validation_errors_path)

    context = common_checks_ocds(context, upload_dir, json_data, schema_ocds)

    if schema_ocds.json_deref_error:
        exceptions.raise_json_deref_error(schema_ocds.json_deref_error)

    context.update(
        {
            "data_schema_version": db_data.data_schema_version,
            "first_render": not db_data.rendered,
            "validation_errors_grouped": group_validation_errors(
                context["validation_errors"]
            ),
        }
    )

    schema_version = getattr(schema_ocds, "version", None)
    if schema_version:
        db_data.schema_version = schema_version
    if not db_data.rendered:
        db_data.rendered = True

    db_data.save()

    if "records" in json_data:
        ocds_show_schema = SchemaOCDS(record_pkg=True)
        ocds_show_deref_schema = ocds_show_schema.get_schema_obj(deref=True)
        template = "cove_ocds/explore_record.html"
        if hasattr(json_data, "get") and hasattr(json_data.get("records"), "__iter__"):
            context["records"] = json_data["records"]
        else:
            context["records"] = []
        if isinstance(json_data["records"], list) and len(json_data["records"]) < 100:
            context["ocds_show_data"] = ocds_show_data(
                json_data, ocds_show_deref_schema
            )
    else:
        ocds_show_schema = SchemaOCDS(record_pkg=False)
        ocds_show_deref_schema = ocds_show_schema.get_schema_obj(deref=True)
        template = "cove_ocds/explore_release.html"
        if hasattr(json_data, "get") and hasattr(json_data.get("releases"), "__iter__"):
            context["releases"] = json_data["releases"]
            if (
                isinstance(json_data["releases"], list)
                and len(json_data["releases"]) < 100
            ):
                context["ocds_show_data"] = ocds_show_data(
                    json_data, ocds_show_deref_schema
                )

            # Parse release dates into objects so the template can format them.
            for release in context["releases"]:
                if hasattr(release, "get") and release.get("date"):
                    if validate_rfc3339(release["date"]):
                        release["date"] = parser.parse(release["date"])
                    else:
                        release["date"] = None
            if context.get("releases_aggregates"):
                date_fields = [
                    "max_award_date",
                    "max_contract_date",
                    "max_release_date",
                    "max_tender_date",
                    "min_award_date",
                    "min_contract_date",
                    "min_release_date",
                    "min_tender_date",
                ]
                for field in date_fields:
                    if context["releases_aggregates"].get(field):
                        if validate_rfc3339(context["releases_aggregates"][field]):
                            context["releases_aggregates"][field] = parser.parse(
                                context["releases_aggregates"][field]
                            )
                        else:
                            context["releases_aggregates"][field] = None
        else:
            context["releases"] = []

    return render(request, template, context)


# This should only be run when data is small.
def ocds_show_data(json_data, ocds_show_deref_schema):
    new_json_data = copy.deepcopy(json_data)
    add_extra_fields(new_json_data, ocds_show_deref_schema)
    return json.dumps(new_json_data, cls=DecimalEncoder)


# From stackoverflow:  https://stackoverflow.com/questions/1960516/python-json-serialize-a-decimal-object
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super(DecimalEncoder, self).default(o)
