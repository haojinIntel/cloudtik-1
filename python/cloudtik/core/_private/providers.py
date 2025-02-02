import copy
import importlib
import logging
import json
import os
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

# For caching provider instantiations across API calls of one python session
_provider_instances = {}

# Minimal config for compatibility with legacy-style external configs.
MINIMAL_EXTERNAL_CONFIG = {
    "available_node_types": {
        "head.default": {},
        "worker.default": {},
    },
    "head_node_type": "head.default",
}


def _import_aws(provider_config):
    from cloudtik.providers._private.aws.node_provider import AWSNodeProvider
    return AWSNodeProvider


def _import_gcp(provider_config):
    from cloudtik.providers._private.gcp.node_provider import GCPNodeProvider
    return GCPNodeProvider


def _import_azure(provider_config):
    from cloudtik.providers._private._azure.node_provider import AzureNodeProvider
    return AzureNodeProvider


def _import_local(provider_config):
    from cloudtik.providers._private.local.node_provider import \
        LocalNodeProvider
    return LocalNodeProvider


def _import_kubernetes(provider_config):
    from cloudtik.providers._private._kubernetes.node_provider import \
        KubernetesNodeProvider
    return KubernetesNodeProvider


def _load_local_defaults_config():
    import cloudtik.providers.local as local_provider
    return os.path.join(os.path.dirname(local_provider.__file__), "defaults.yaml")


def _load_kubernetes_defaults_config():
    import cloudtik.providers.kubernetes as kubernetes_provider
    return os.path.join(
        os.path.dirname(kubernetes_provider.__file__), "defaults.yaml")


def _load_aws_defaults_config():
    import cloudtik.providers.aws as aws_provider
    return os.path.join(os.path.dirname(aws_provider.__file__), "defaults.yaml")


def _load_gcp_defaults_config():
    import cloudtik.providers.gcp as gcp_provider
    return os.path.join(os.path.dirname(gcp_provider.__file__), "defaults.yaml")


def _load_azure_defaults_config():
    import cloudtik.providers.azure as azure_provider
    return os.path.join(os.path.dirname(azure_provider.__file__), "defaults.yaml")


def _import_external(provider_config):
    provider_cls = _load_class(path=provider_config["module"])
    return provider_cls


_NODE_PROVIDERS = {
    "local": _import_local,
    "aws": _import_aws,
    "gcp": _import_gcp,
    "azure": _import_azure,
    "kubernetes": _import_kubernetes,
    "external": _import_external  # Import an external module
}

_PROVIDER_PRETTY_NAMES = {
    "local": "Local",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "kubernetes": "Kubernetes",
    "external": "External"
}

_DEFAULT_CONFIGS = {
    "local": _load_local_defaults_config,
    "aws": _load_aws_defaults_config,
    "gcp": _load_gcp_defaults_config,
    "azure": _load_azure_defaults_config,
    "kubernetes": _load_kubernetes_defaults_config,
}

# For caching workspace provider instantiations across API calls of one python session
_workspace_provider_instances = {}


def _import_aws_workspace(provider_config):
    from cloudtik.providers._private.aws.workspace_provider import AWSWorkspaceProvider
    return AWSWorkspaceProvider


def _import_gcp_workspace(provider_config):
    from cloudtik.providers._private.gcp.workspace_provider import GCPWorkspaceProvider
    return GCPWorkspaceProvider


def _import_azure_workspace(provider_config):
    from cloudtik.providers._private._azure.workspace_provider import AzureWorkspaceProvider
    return AzureWorkspaceProvider


def _import_local_workspace(provider_config):
    from cloudtik.providers._private.local.workspace_provider import \
        LocalWorkspaceProvider
    return LocalWorkspaceProvider


def _import_kubernetes_workspace(provider_config):
    from cloudtik.providers._private._kubernetes.workspace_provider import \
        KubernetesWorkspaceProvider
    return KubernetesWorkspaceProvider


def _load_local_workspace_defaults_config():
    import cloudtik.providers.local as local_provider
    return os.path.join(os.path.dirname(local_provider.__file__), "workspace-defaults.yaml")


def _load_kubernetes_workspace_defaults_config():
    import cloudtik.providers.kubernetes as kubernetes_provider
    return os.path.join(
        os.path.dirname(kubernetes_provider.__file__), "workspace-defaults.yaml")


def _load_aws_workspace_defaults_config():
    import cloudtik.providers.aws as aws_provider
    return os.path.join(os.path.dirname(aws_provider.__file__), "workspace-defaults.yaml")


def _load_gcp_workspace_defaults_config():
    import cloudtik.providers.gcp as gcp_provider
    return os.path.join(os.path.dirname(gcp_provider.__file__), "workspace-defaults.yaml")


def _load_azure_workspace_defaults_config():
    import cloudtik.providers.azure as azure_provider
    return os.path.join(os.path.dirname(azure_provider.__file__), "workspace-defaults.yaml")


_WORKSPACE_PROVIDERS = {
    "local": _import_local_workspace,
    "aws": _import_aws_workspace,
    "gcp": _import_gcp_workspace,
    "azure": _import_azure_workspace,
    "kubernetes": _import_kubernetes_workspace,
    "external": _import_external  # Import an external module
}

_DEFAULT_WORKSPACE_CONFIGS = {
    "local": _load_local_workspace_defaults_config,
    "aws": _load_aws_workspace_defaults_config,
    "gcp": _load_gcp_workspace_defaults_config,
    "azure": _load_azure_workspace_defaults_config,
    "kubernetes": _load_kubernetes_workspace_defaults_config,
}


def _load_class(path):
    """Load a class at runtime given a full path.

    Example of the path: mypkg.mysubpkg.myclass
    """
    class_data = path.split(".")
    if len(class_data) < 2:
        raise ValueError(
            "You need to pass a valid path like mymodule.provider_class")
    module_path = ".".join(class_data[:-1])
    class_str = class_data[-1]
    module = importlib.import_module(module_path)
    return getattr(module, class_str)


def _get_node_provider_cls(provider_config: Dict[str, Any]):
    """Get the node provider class for a given provider config.

    Note that this may be used by private node providers that proxy methods to
    built-in node providers, so we should maintain backwards compatibility.

    Args:
        provider_config: provider section of the cluster config.

    Returns:
        NodeProvider class
    """
    importer = _NODE_PROVIDERS.get(provider_config["type"])
    if importer is None:
        raise NotImplementedError("Unsupported node provider: {}".format(
            provider_config["type"]))
    return importer(provider_config)


def _get_node_provider(provider_config: Dict[str, Any],
                       cluster_name: str,
                       use_cache: bool = True) -> Any:
    """Get the instantiated node provider for a given provider config.

    Note that this may be used by private node providers that proxy methods to
    built-in node providers, so we should maintain backwards compatibility.

    Args:
        provider_config: provider section of the cluster config.
        cluster_name: cluster name from the cluster config.
        use_cache: whether or not to use a cached definition if available. If
            False, the returned object will also not be stored in the cache.

    Returns:
        NodeProvider
    """
    provider_key = (json.dumps(provider_config, sort_keys=True), cluster_name)
    if use_cache and provider_key in _provider_instances:
        return _provider_instances[provider_key]

    provider_cls = _get_node_provider_cls(provider_config)
    new_provider = provider_cls(provider_config, cluster_name)

    if use_cache:
        _provider_instances[provider_key] = new_provider

    return new_provider


def _clear_provider_cache():
    global _provider_instances
    _provider_instances = {}


def _get_default_config(provider_config):
    """Retrieve a node provider.

    This is an INTERNAL API. It is not allowed to call this from outside.
    """
    if provider_config["type"] == "external":
        return copy.deepcopy(MINIMAL_EXTERNAL_CONFIG)
    load_config = _DEFAULT_CONFIGS.get(provider_config["type"])
    if load_config is None:
        raise NotImplementedError("Unsupported node provider: {}".format(
            provider_config["type"]))
    path_to_default = load_config()
    with open(path_to_default) as f:
        defaults = yaml.safe_load(f)

    return defaults


def _get_workspace_provider_cls(provider_config: Dict[str, Any]):
    """Get the workspace provider class for a given provider config.

    Note that this may be used by private workspace providers that proxy methods to
    built-in workspace providers, so we should maintain backwards compatibility.

    Args:
        provider_config: provider section of the workspace config.

    Returns:
        WorkspaceProvider class
    """
    importer = _WORKSPACE_PROVIDERS.get(provider_config["type"])
    if importer is None:
        raise NotImplementedError("Unsupported workspace provider: {}".format(
            provider_config["type"]))
    return importer(provider_config)


def _get_workspace_provider(provider_config: Dict[str, Any],
                       workspace_name: str,
                       use_cache: bool = True) -> Any:
    """Get the instantiated workspace provider for a given provider config.

    Note that this may be used by private workspace providers that proxy methods to
    built-in workspace providers, so we should maintain backwards compatibility.

    Args:
        provider_config: provider section of the cluster config.
        workspace_name: workspace name from the cluster config.
        use_cache: whether or not to use a cached definition if available. If
            False, the returned object will also not be stored in the cache.

    Returns:
        WorkspaceProvider
    """
    provider_key = (json.dumps(provider_config, sort_keys=True), workspace_name)
    if use_cache and provider_key in _workspace_provider_instances:
        return _workspace_provider_instances[provider_key]

    provider_cls = _get_workspace_provider_cls(provider_config)
    new_provider = provider_cls(provider_config, workspace_name)

    if use_cache:
        _workspace_provider_instances[provider_key] = new_provider

    return new_provider


def _clear_workspace_provider_cache():
    global _workspace_provider_instances
    _workspace_provider_instances = {}


def _get_default_workspace_config(provider_config):
    """Retrieve the default workspace config.

    This is an INTERNAL API. It is not allowed to call this from outside.
    """
    # TODO: stiill not check with exterbal type
    if provider_config["type"] == "external":
        return copy.deepcopy(MINIMAL_EXTERNAL_CONFIG)
    load_config = _DEFAULT_WORKSPACE_CONFIGS.get(provider_config["type"])
    if load_config is None:
        raise NotImplementedError("Unsupported workspace provider: {}".format(
            provider_config["type"]))
    path_to_default = load_config()
    with open(path_to_default) as f:
        defaults = yaml.safe_load(f)

    return defaults
