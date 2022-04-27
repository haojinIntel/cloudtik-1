import os
from typing import Any, Dict

from cloudtik.core._private.utils import merge_rooted_config_hierarchy, _get_runtime_config_object

RUNTIME_PROCESSES = [
    # The first element is the substring to filter.
    # The second element, if True, is to filter ps results by command name.
    # The third element is the process name.
    # The forth element, if node, the process should on all nodes,if head, the process should on head node.
    ["proc_namenode", False, "NameNode", "head"],
    ["proc_datanode", False, "DataNode", "worker"],
]

RUNTIME_ROOT_PATH = os.path.abspath(os.path.dirname(__file__))


def _config_runtime_resources(cluster_config: Dict[str, Any]) -> Dict[str, Any]:
    return cluster_config


def _config_runtime_tags(cluster_config: Dict[str, Any]) -> Dict[str, Any]:
    cluster_runtime_tag = cluster_config.get("runtime").get("tags", {})
    cluster_runtime_tag["NAMENODE_ADDRESS"] = "HEAD_ADDRESS"
    cluster_config["runtime"]["tags"] = cluster_runtime_tag
    return cluster_config


def _get_runtime_processes():
    return RUNTIME_PROCESSES


def _is_runtime_scripts(script_file):
    return False


def _get_runnable_command(target):
    return None


def _with_runtime_environment_variables(runtime_config, provider):
    runtime_envs = {"HDFS_ENABLED": True}
    return runtime_envs


def _get_runtime_logs():
    hadoop_logs_dir = os.path.join(os.getenv("HADOOP_HOME"), "logs")
    all_logs = {"hadoop": hadoop_logs_dir}
    return all_logs


def _validate_config(config: Dict[str, Any], provider):
    pass


def _verify_config(config: Dict[str, Any], provider):
    pass


def _get_config_object(cluster_config: Dict[str, Any], object_name: str) -> Dict[str, Any]:
    config_root = os.path.join(RUNTIME_ROOT_PATH, "config")
    runtime_commands = _get_runtime_config_object(config_root, cluster_config["provider"], object_name)
    return merge_rooted_config_hierarchy(config_root, runtime_commands, object_name)


def _get_runtime_commands(cluster_config: Dict[str, Any]) -> Dict[str, Any]:
    return _get_config_object(cluster_config, "commands")


def _get_defaults_config(cluster_config: Dict[str, Any]) -> Dict[str, Any]:
    return _get_config_object(cluster_config, "defaults")


def _get_custom_config(cluster_config: Dict[str, Any]) -> Dict[str, Any]:
    hdfs_config = cluster_config.get("runtime", {}).get("hdfs", {})
    namenode_address = hdfs_config.get("namenode_address")
    if namenode_address is None:
        return None
    else:
        return {"namenode_address": namenode_address}


def _get_useful_urls(cluster_head_ip):
    urls = [
        {"name": "HDFS Web UI", "url": "http://{}:9870".format(cluster_head_ip)},
    ]
    return urls
