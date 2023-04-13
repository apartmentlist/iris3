"""Entry point for Iris."""
import sys

print("Initializing ", file=sys.stderr)

from util.utils import init_logging

# Must init logging before any library code writes logs (which would overwide our config)
init_logging()

from functools import lru_cache
import google.appengine.api
from typing import Dict, Type

import time

cold_start_begin = time.time()
import base64
import json
import logging
import os

import flask

from plugin import Plugin, PluginHolder
from util import pubsub_utils, gcp_utils, utils, config_utils
from util.gcp_utils import (
    detect_gae,
    is_appscript_project,
    all_projects,
    current_project_id,
    gae_memory_logging,
)

from util.config_utils import (
    iris_prefix,
    is_project_enabled,
    get_config,
    pubsub_token,
    is_in_test_or_dev_project,
    is_test_or_dev_configuration,
)
from util.utils import log_time, timing


ENABLE_PROFILER = False


def __enable_cloudprofiler():
    """# For Google Cloud Profiler,
    * set ENABLE_PROFILER to True above
    * edit requirements.txt as stated in requirements.txt
    * add a line to app.yaml as stated in requirements.txt
    """
    try:
        import googlecloudprofiler

        googlecloudprofiler.start()
    except (ValueError, NotImplementedError) as exc:
        localdev_error_msg = (
            ". Profiler is not supported in local development"
            if "Service name must be provided" in str(exc)
            else ""
        )

        logging.info(
            "Exception initializing the Cloud Profiler %s, %s", exc, localdev_error_msg
        )


# Profiler initialization. It starts a daemon thread which continuously collects and uploads profiles.
if detect_gae() and ENABLE_PROFILER:
    __enable_cloudprofiler()
else:
    logging.info("Cloud Profiler not in use")

gcp_utils.set_env()

logging.info(
    "env GAE_USE_SOCKETS_HTTPLIB is %s", os.environ.get("GAE_USE_SOCKETS_HTTPLIB")
)

app = flask.Flask(__name__)
if detect_gae():
    app.wsgi_app = google.appengine.api.wrap_wsgi_app(app.wsgi_app)

PluginHolder.init()


@app.route("/")
def index():
    with gae_memory_logging("index"):
        msg = f"I'm {iris_prefix().capitalize()}, pleased to meet you!"
        logging.info("index() ")

        return msg, 200


@app.route("/_ah/warmup")
def warmup():
    with gae_memory_logging("warmup"):
        logging.info("warmup() called")

        return "", 200, {}


@app.route("/schedule", methods=["GET"])
@log_time
def schedule():
    """
    Send out a message per-plugin per-project to label all objects of that type and project.
    """
    with gae_memory_logging("schedule"):

        try:
            logging.info("Schedule called")

            is_cron = flask.request.headers.get("X-Appengine-Cron")
            if not is_cron:
                return "Access Denied: No Cron header found", 403

            enabled_projects = __get_enabled_projects()
            __send_pubsub_per_projectplugin(enabled_projects)
            return "OK", 200
        except Exception as e:
            logging.exception("In schedule()", exc_info=e)
            return "Error", 500


@lru_cache(maxsize=1)
def __get_enabled_projects():
    configured_as_enabled = config_utils.enabled_projects()
    if configured_as_enabled:
        enabled_projs = configured_as_enabled
    else:
        all_proj = all_projects()
        # In my testing, we do NOT get appscript projects in the list.
        # There is a small chance that with other permissions, these appscript projects would appear.
        # so here we filter them out.

        nonappscript_projects = (p for p in all_proj if not is_appscript_project(p))

        enabled_only = (
            p for p in nonappscript_projects if config_utils.is_project_enabled(p)
        )
        enabled_projs = list(enabled_only)
    enabled_projs.sort()
    if not enabled_projs:
        raise Exception("No projects enabled at all")

    if (
        not detect_gae()
        or is_test_or_dev_configuration()
        or is_in_test_or_dev_project(current_project_id())
    ):
        max_proj_in_dev = 3
        if len(enabled_projs) > max_proj_in_dev:
            raise Exception(
                """In development or testing, we support no more than %d projects
                    to avoid accidentally flooding the system. 
                    %d projects are available, which exceeds that
                   """
                % (
                    max_proj_in_dev,
                    len(enabled_projs),
                )
            )
    return enabled_projs


def __send_pubsub_per_projectplugin(configured_projects):
    msg_count = 0
    for project_id in configured_projects:
        for plugin_cls in PluginHolder.plugins:
            if (
                not plugin_cls.is_labeled_on_creation()
                or plugin_cls.relabel_on_cron()
                or config_utils.label_all_on_cron()
            ):
                pubsub_utils.publish(
                    msg=json.dumps(
                        {"project_id": project_id, "plugin": plugin_cls.__name__}
                    ),
                    topic_id=pubsub_utils.schedulelabeling_topic(),
                )

                logging.info(
                    "Sent do_label message for %s , %s",
                    project_id,
                    plugin_cls.__name__,
                )
            msg_count += 1
    logging.info(
        "schedule() sent %d messages to label %d projects",
        msg_count,
        len(configured_projects),
    )


@app.route("/label_one", methods=["POST"])
def label_one():
    with gae_memory_logging("label_one"):

        plugins_found = []
        data = {}
        try:
            """
            PubSub push endpoint for messages from the Log Sink
            """
            # Performance question: There are multiple log lines for each object-creation, for example,
            # one for request and one for response. So, we may be labeling each object multiple times,
            # which is a waste of resources.
            #
            # Or maybe not. Maybe the first PubSub-triggered action fails, because the resource is not initialized, and
            # then the second one succeeds; need to check that.

            data = __extract_pubsub_content()

            method_from_log = data["protoPayload"]["methodName"]

            for plugin_cls in PluginHolder.plugins.keys():
                method_names = plugin_cls.method_names()

                for supported_method in method_names:
                    if supported_method.lower() in method_from_log.lower():
                        if plugin_cls.is_labeled_on_creation():
                            __label_one_0(data, plugin_cls)

                        plugins_found.append(
                            plugin_cls.__name__
                        )  # Append it even if not used due to is_labeled_on_creation False

            if not plugins_found:
                logging.info(
                    "(OK if plugin is disabled.) No plugins found for %s. Enabled plugins are %s",
                    method_from_log,
                    config_utils.enabled_plugins(),
                )

            if len(plugins_found) > 1:
                raise Exception(
                    "Error: Multiple plugins found %s for %s"
                    % (plugins_found, method_from_log)
                )

            return "OK", 200
        except Exception as e:
            project_id = data.get("resource", {}).get("labels", {}).get("project_id")
            logging.exception(
                "Error on label_one %s %s", plugins_found, project_id, exc_info=e
            )
            return "Error", 500


def __label_one_0(data, plugin_cls: Type[Plugin]):
    plugin = PluginHolder.get_plugin_instance(plugin_cls)
    gcp_object = plugin.get_gcp_object(data)
    if gcp_object is not None:
        project_id = data["resource"]["labels"]["project_id"]
        if is_project_enabled(project_id):
            logging.info(
                "Will label_one() in %s, existing object %s...",
                project_id,
                str(gcp_object)[:100],
            )
            plugin.label_resource(gcp_object, project_id)
            plugin.do_batch()
        else:
            msg = (
                f"Skipping label_one({plugin_cls.__name__}) in unsupported "
                f"project {project_id}; (Should not get here in current design, since the Sink filter should only include "
                f"supported projects; also, schedule() already filters for the enabled projects. "
                f"However, if the Sink filter was not updated to match config.yaml, or in local development"
                f"if a command is given to label an arbitrary project that is not in config.yaml, this can happen"
            )
            logging.info(msg)
    else:
        logging.error(
            "Cannot find gcp_object to label. (This usually does sometimes does not result in failure to label, "
            + "e.g. for BQ datasets where serviceData is missing), based on %s",
            utils.shorten(str(data.get("resource")), 300),
        )


def __extract_pubsub_content() -> Dict:
    """Take the value at the relevant key in the logging message from PubSub,
    Base64-decode, convert to Python object."""
    __check_pubsub_verification_token()

    envelope = flask.request.get_json()
    msg = envelope.get("message", {})

    logging.info(
        "PubSub deliveryAttempt %s; messageId %s, timestamp %s",
        envelope.get("deliveryAttempt", "N/A"),
        msg.get("messageId", "N/A"),
        msg.get("publishTime", "N/A"),
    )

    if not envelope:
        raise FlaskException("Expect JSON, was empty")

    data = json.loads(base64.b64decode(envelope["message"]["data"]))
    return data


@app.route("/do_label", methods=["POST"])
def do_label():
    with gae_memory_logging("do_label"):

        """Receive a push message from PubSub, sent from schedule() above,
        with instructions to label all objects of a given plugin and project_id.
        """
        project_id = ""  # set up variables to allow logging in Exception block at end
        plugin_class_name = ""
        try:
            data = __extract_pubsub_content()
            plugin_class_name = data["plugin"]

            plugin = PluginHolder.get_plugin_instance_by_name(plugin_class_name)
            if not plugin:
                logging.info(
                    "(OK if plugin is disabled.) No plugins found for %s. Enabled plugins are %s",
                    plugin_class_name,
                    config_utils.enabled_plugins(),
                )
            else:
                project_id = data["project_id"]
                with timing(f"do_label {plugin_class_name} {project_id}"):
                    logging.info(
                        "do_label() for %s in %s", plugin.__class__.__name__, project_id
                    )
                    plugin.label_all(project_id)
                logging.info("OK on do_label %s %s", plugin_class_name, project_id)
            return "OK", 200
        except Exception as e:
            logging.exception(
                "Error on do_label %s %s", plugin_class_name, project_id, exc_info=e
            )
            return "Error", 500


def __check_pubsub_verification_token():
    """Token verifying that only PubSub accesses PubSub push endpoints"""
    known_token = pubsub_token()
    if not known_token:
        raise FlaskException(
            f"Should define expected token in env. Keys were {list(os.environ.keys())}",
            400,
        )

    token_from_args = flask.request.args.get("token", "")
    if known_token != token_from_args:
        raise FlaskException(f'Access denied: Invalid token "{known_token}"', 403)


class FlaskException(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv["message"] = self.message
        return rv


@app.errorhandler(FlaskException)
def handle_invalid_usage(error):
    logging.exception("", exc_info=error)
    response = flask.jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


logging.info(f"Coldstart took {int((time.time() - cold_start_begin) * 1000)} ms")

if __name__ in ["__main__"]:
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    port = os.environ.get("PORT", 8000)
    logging.info("Running __main__ for main.py, port %s", port)
    app.run(host="127.0.0.1", port=port, debug=True)
