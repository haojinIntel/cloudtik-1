import click
import os
import logging
from typing import Any, Dict
import yaml
from cloudtik.core._private import constants

from cloudtik.core._private import logging_utils

from cloudtik.core._private.cli_logger import (cli_logger)

from shlex import quote

CLOUDTIK_RUNTIME_PATH = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

CLOUDTIK_RUNTIME_SCRIPTS_PATH = os.path.join(
    CLOUDTIK_RUNTIME_PATH, "spark/scripts/")

SPARK_SERVICES_SCRIPT_PATH = os.path.join(CLOUDTIK_RUNTIME_SCRIPTS_PATH, "services.sh")
SPARK_OUT_CONF = os.path.join(CLOUDTIK_RUNTIME_PATH, "spark/conf/outconf/spark/spark-defaults.conf")
logger = logging.getLogger(__name__)


def _get_spark_config(config: Dict[str, Any]):
    runtime = config.get("runtime")
    if not runtime:
        return None

    spark = runtime.get("spark")
    if not spark:
        return None

    return spark.get("config")


@click.group()
@click.option(
    "--logging-level",
    required=False,
    default=constants.LOGGER_LEVEL,
    type=str,
    help=constants.LOGGER_LEVEL_HELP)
@click.option(
    "--logging-format",
    required=False,
    default=constants.LOGGER_FORMAT,
    type=str,
    help=constants.LOGGER_FORMAT_HELP)
@click.version_option()
def cli(logging_level, logging_format):
    level = logging.getLevelName(logging_level.upper())
    logging_utils.setup_logger(level, logging_format)
    cli_logger.set_format(format_tmpl=logging_format)


@click.command()
def install():
    install_script_path = os.path.join(CLOUDTIK_RUNTIME_SCRIPTS_PATH, "install.sh")
    os.system("bash {}".format(install_script_path))


@click.command()
@click.option(
    "--head",
    is_flag=True,
    default=False,
    help="provide this argument for the head node")
@click.option(
    '--provider',
    required=True,
    type=str,
    help="the provider of cluster ")
@click.option(
    '--head_address',
    required=False,
    type=str,
    default="",
    help="the head ip ")
@click.option(
    '--aws_s3a_bucket',
    required=False,
    type=str,
    default="",
    help="the bucket name of s3a")
@click.option(
    '--s3a_access_key',
    required=False,
    type=str,
    default="",
    help="the access key of s3a")
@click.option(
    '--s3a_secret_key',
    required=False,
    type=str,
    default="",
    help="the secret key of s3a")
@click.option(
    '--project_id',
    required=False,
    type=str,
    default="",
    help="gcp project id")
@click.option(
    '--gcp_gcs_bucket',
    required=False,
    type=str,
    default="",
    help="gcp cloud storage bucket name")
@click.option(
    '--fs_gs_auth_service_account_email',
    required=False,
    type=str,
    default="",
    help="google service account email")
@click.option(
    '--fs_gs_auth_service_account_private_key_id',
    required=False,
    type=str,
    default="",
    help="google service account private key id")
@click.option(
    '--fs_gs_auth_service_account_private_key',
    required=False,
    type=str,
    default="",
    help="google service account private key")
@click.option(
    '--azure_storage_kind',
    required=False,
    type=str,
    default="",
    help="azure storage kind, whether azure blob storage or azure data lake gen2")
@click.option(
    '--azure_storage_account',
    required=False,
    type=str,
    default="",
    help="azure storage account")
@click.option(
    '--azure_container',
    required=False,
    type=str,
    default="",
    help="azure storage container")
@click.option(
    '--azure_account_key',
    required=False,
    type=str,
    default="",
    help="azure storage account access key")
def configure(head, provider, head_address, aws_s3a_bucket, s3a_access_key, s3a_secret_key, project_id, gcp_gcs_bucket,
              fs_gs_auth_service_account_email, fs_gs_auth_service_account_private_key_id,
              fs_gs_auth_service_account_private_key, azure_storage_kind, azure_storage_account, azure_container,
              azure_account_key):
    shell_path = os.path.join(CLOUDTIK_RUNTIME_SCRIPTS_PATH, "configure.sh")
    cmds = [
        "bash",
        shell_path,
    ]

    if head:
        cmds += ["--head"]
    if provider:
        cmds += ["--provider={}".format(provider)]
    if head_address:
        cmds += ["--head_address={}".format(head_address)]

    if aws_s3a_bucket:
        cmds += ["--aws_s3a_bucket={}".format(aws_s3a_bucket)]
    if s3a_access_key:
        cmds += ["--s3a_access_key={}".format(s3a_access_key)]
    if s3a_secret_key:
        cmds += ["--s3a_secret_key={}".format(s3a_secret_key)]

    if project_id:
        cmds += ["--project_id={}".format(project_id)]
    if gcp_gcs_bucket:
        cmds += ["--gcp_gcs_bucket={}".format(gcp_gcs_bucket)]

    if fs_gs_auth_service_account_email:
        cmds += ["--fs_gs_auth_service_account_email={}".format(fs_gs_auth_service_account_email)]
    if fs_gs_auth_service_account_private_key_id:
        cmds += ["--fs_gs_auth_service_account_private_key_id={}".format(fs_gs_auth_service_account_private_key_id)]
    if fs_gs_auth_service_account_private_key:
        cmds += ["--fs_gs_auth_service_account_private_key={}".format(quote(fs_gs_auth_service_account_private_key))]

    if azure_storage_kind:
        cmds += ["--azure_storage_kind={}".format(azure_storage_kind)]
    if azure_storage_account:
        cmds += ["--azure_storage_account={}".format(azure_storage_account)]
    if azure_container:
        cmds += ["--azure_container={}".format(azure_container)]
    if azure_account_key:
        cmds += ["--azure_account_key={}".format(azure_account_key)]

    final_cmd = " ".join(cmds)
    os.system(final_cmd)
    # Merge user specified configuration and default configuration
    bootstrap_config = os.path.expanduser("~/cloudtik_bootstrap_config.yaml")
    if os.path.exists(bootstrap_config):
        config = yaml.safe_load(open(bootstrap_config).read())
        spark_config = _get_spark_config(config)
        if spark_config:
            spark_conf = {}
            with open(SPARK_OUT_CONF, "r") as f:
                for line in f.readlines():
                    if not line.startswith("#"):
                        key, value = line.split(" ")
                        spark_conf[key] = value
            spark_conf.update(spark_config)
            with open(os.path.join(os.getenv("SPARK_HOME"), "conf/spark-defaults.conf"), "w+") as f:
                for key, value in spark_conf.items():
                    f.write("{}    {}\n".format(key, value))


@click.command()
def start_head():
    os.system("bash {} start-head".format(SPARK_SERVICES_SCRIPT_PATH))


@click.command()
def start_worker():
    os.system("bash {} start-worker".format(SPARK_SERVICES_SCRIPT_PATH))


@click.command()
def stop_head():
    os.system("bash {} stop-head".format(SPARK_SERVICES_SCRIPT_PATH))


@click.command()
def stop_worker():
    os.system("bash {} stop-worker".format(SPARK_SERVICES_SCRIPT_PATH))


cli.add_command(install)
cli.add_command(configure)
cli.add_command(start_head)
cli.add_command(start_worker)
cli.add_command(stop_head)
cli.add_command(stop_worker)


def main():
    return cli()


if __name__ == "__main__":
    main()
