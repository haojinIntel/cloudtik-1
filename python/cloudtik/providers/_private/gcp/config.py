import copy
from functools import partial
import json
import os
import logging
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from googleapiclient import discovery, errors
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials

from cloudtik.providers._private.gcp.node import (GCPNodeType, MAX_POLLS,
                                              POLL_INTERVAL)

from cloudtik.core._private.cli_logger import cli_logger, cf
from cloudtik.core._private.services import get_node_ip_address
from cloudtik.core._private.utils import check_cidr_conflict

logger = logging.getLogger(__name__)

VERSION = "v1"
TPU_VERSION = "v2alpha"  # change once v2 is stable

CLOUDTIK = "cloudtik"
CLOUDTIK_DEFAULT_SERVICE_ACCOUNT_ID = CLOUDTIK + "-sa-" + VERSION
SERVICE_ACCOUNT_EMAIL_TEMPLATE = (
    "{account_id}@{project_id}.iam.gserviceaccount.com")
DEFAULT_SERVICE_ACCOUNT_CONFIG = {
    "displayName": "CloudTik Service Account ({})".format(VERSION),
}

# Those roles will be always added.
DEFAULT_SERVICE_ACCOUNT_ROLES = [
    "roles/storage.objectAdmin", "roles/compute.admin",
    "roles/iam.serviceAccountUser"
]
# Those roles will only be added if there are TPU nodes defined in config.
TPU_SERVICE_ACCOUNT_ROLES = ["roles/tpu.admin"]

# If there are TPU nodes in config, this field will be set
# to True in config["provider"].
HAS_TPU_PROVIDER_FIELD = "_has_tpus"

# NOTE: iam.serviceAccountUser allows the Head Node to create worker nodes
# with ServiceAccounts.

NUM_GCP_WORKSPACE_CREATION_STEPS = 6
NUM_GCP_WORKSPACE_DELETION_STEPS = 4


def get_node_type(node: dict) -> GCPNodeType:
    """Returns node type based on the keys in ``node``.

    This is a very simple check. If we have a ``machineType`` key,
    this is a Compute instance. If we don't have a ``machineType`` key,
    but we have ``acceleratorType``, this is a TPU. Otherwise, it's
    invalid and an exception is raised.

    This works for both node configs and API returned nodes.
    """

    if "machineType" not in node and "acceleratorType" not in node:
        raise ValueError(
            "Invalid node. For a Compute instance, 'machineType' is "
            "required. "
            "For a TPU instance, 'acceleratorType' and no 'machineType' "
            "is required. "
            f"Got {list(node)}")

    if "machineType" not in node and "acceleratorType" in node:
        # remove after TPU pod support is added!
        if node["acceleratorType"] not in ("v2-8", "v3-8"):
            raise ValueError(
                "For now, only v2-8' and 'v3-8' accelerator types are "
                "supported. Support for TPU pods will be added in the future.")

        return GCPNodeType.TPU
    return GCPNodeType.COMPUTE


def wait_for_crm_operation(operation, crm):
    """Poll for cloud resource manager operation until finished."""
    logger.info("wait_for_crm_operation: "
                "Waiting for operation {} to finish...".format(operation))

    for _ in range(MAX_POLLS):
        result = crm.operations().get(name=operation["name"]).execute()
        if "error" in result:
            raise Exception(result["error"])

        if "done" in result and result["done"]:
            logger.info("wait_for_crm_operation: Operation done.")
            break

        time.sleep(POLL_INTERVAL)

    return result


def wait_for_compute_global_operation(project_name, operation, compute):
    """Poll for global compute operation until finished."""
    logger.info("wait_for_compute_global_operation: "
                "Waiting for operation {} to finish...".format(
                    operation["name"]))

    for _ in range(MAX_POLLS):
        result = compute.globalOperations().get(
            project=project_name,
            operation=operation["name"],
        ).execute()
        if "error" in result:
            raise Exception(result["error"])

        if result["status"] == "DONE":
            logger.info("wait_for_compute_global_operation: "
                        "Operation done.")
            break

        time.sleep(POLL_INTERVAL)

    return result


def key_pair_name(i, region, project_id, ssh_user):
    """Returns the ith default gcp_key_pair_name."""
    key_name = "{}_gcp_{}_{}_{}_{}".format(CLOUDTIK, region, project_id, ssh_user,
                                           i)
    return key_name


def key_pair_paths(key_name):
    """Returns public and private key paths for a given key_name."""
    public_key_path = os.path.expanduser("~/.ssh/{}.pub".format(key_name))
    private_key_path = os.path.expanduser("~/.ssh/{}.pem".format(key_name))
    return public_key_path, private_key_path


def generate_rsa_key_pair():
    """Create public and private ssh-keys."""

    key = rsa.generate_private_key(
        backend=default_backend(), public_exponent=65537, key_size=2048)

    public_key = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH).decode("utf-8")

    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()).decode("utf-8")

    return public_key, pem


def _has_tpus_in_node_configs(config: dict) -> bool:
    """Check if any nodes in config are TPUs."""
    node_configs = [
        node_type["node_config"]
        for node_type in config["available_node_types"].values()
    ]
    return any(get_node_type(node) == GCPNodeType.TPU for node in node_configs)


def _is_head_node_a_tpu(config: dict) -> bool:
    """Check if the head node is a TPU."""
    node_configs = {
        node_id: node_type["node_config"]
        for node_id, node_type in config["available_node_types"].items()
    }
    return get_node_type(
        node_configs[config["head_node_type"]]) == GCPNodeType.TPU


def _create_crm(gcp_credentials=None):
    return discovery.build(
        "cloudresourcemanager",
        "v1",
        credentials=gcp_credentials,
        cache_discovery=False)


def _create_iam(gcp_credentials=None):
    return discovery.build(
        "iam", "v1", credentials=gcp_credentials, cache_discovery=False)


def _create_compute(gcp_credentials=None):
    return discovery.build(
        "compute", "v1", credentials=gcp_credentials, cache_discovery=False)


def _create_tpu(gcp_credentials=None):
    return discovery.build(
        "tpu",
        TPU_VERSION,
        credentials=gcp_credentials,
        cache_discovery=False,
        discoveryServiceUrl="https://tpu.googleapis.com/$discovery/rest")


def construct_clients_from_provider_config(provider_config):
    """
    Attempt to fetch and parse the JSON GCP credentials from the provider
    config yaml file.

    tpu resource (the last element of the tuple) will be None if
    `_has_tpus` in provider config is not set or False.
    """
    gcp_credentials = provider_config.get("gcp_credentials")
    if gcp_credentials is None:
        logger.debug("gcp_credentials not found in cluster yaml file. "
                     "Falling back to GOOGLE_APPLICATION_CREDENTIALS "
                     "environment variable.")
        tpu_resource = _create_tpu() if provider_config.get(
            HAS_TPU_PROVIDER_FIELD, False) else None
        # If gcp_credentials is None, then discovery.build will search for
        # credentials in the local environment.
        return _create_crm(), \
            _create_iam(), \
            _create_compute(), \
            tpu_resource

    assert ("type" in gcp_credentials), \
        "gcp_credentials cluster yaml field missing 'type' field."
    assert ("credentials" in gcp_credentials), \
        "gcp_credentials cluster yaml field missing 'credentials' field."

    cred_type = gcp_credentials["type"]
    credentials_field = gcp_credentials["credentials"]

    if cred_type == "service_account":
        # If parsing the gcp_credentials failed, then the user likely made a
        # mistake in copying the credentials into the config yaml.
        try:
            service_account_info = json.loads(credentials_field)
        except json.decoder.JSONDecodeError:
            raise RuntimeError(
                "gcp_credentials found in cluster yaml file but "
                "formatted improperly.")
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info)
    elif cred_type == "credentials_token":
        # Otherwise the credentials type must be credentials_token.
        credentials = OAuthCredentials(credentials_field)

    tpu_resource = _create_tpu(credentials) if provider_config.get(
        HAS_TPU_PROVIDER_FIELD, False) else None

    return _create_crm(credentials), \
        _create_iam(credentials), \
        _create_compute(credentials), \
        tpu_resource


def create_gcp_workspace(config):
    config = copy.deepcopy(config)

    # Steps of configuring the workspace
    config = _configure_workspace(config)

    return config


def _configure_workspace(config):
    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])
    workspace_name = config["workspace_name"]

    current_step = 1
    total_steps = NUM_GCP_WORKSPACE_CREATION_STEPS
    try:
        with cli_logger.group("Creating workspace: {}", workspace_name):
            with cli_logger.group(
                    "Configuring project",
                    _numbered=("[]", current_step, total_steps)):
                current_step += 1
                config = _configure_project(config, crm)
            config = _configure_network_resources(config, current_step, total_steps)
    except Exception as e:
        cli_logger.error("Failed to create workspace. {}", str(e))
        raise e

    cli_logger.print(
        "Successfully created workspace: {}.",
        cf.bold(workspace_name))

    return config


def get_workspace_vpc_id(config, compute):
    project_id = config["provider"].get("project_id")
    vpc_name = 'cloudtik-{}-vpc'.format(config["workspace_name"])
    cli_logger.verbose("Getting the VpcId for workspace: {}...".
                     format(vpc_name))

    VpcIds = [vpc["id"] for vpc in compute.networks().list(project=project_id).execute().get("items", "")
           if vpc["name"] == vpc_name]
    if len(VpcIds) == 0:
        cli_logger.verbose("The VPC for workspace is not found: {}.".
                         format(vpc_name))
        return None
    else:
        cli_logger.verbose_error("Successfully get the VpcId of {} for workspace.".
                         format(vpc_name))
        return VpcIds[0]


def _delete_vpc(config, compute):
    VpcId = get_workspace_vpc_id(config, compute)
    project_id = config["provider"].get("project_id")
    vpc_name = 'cloudtik-{}-vpc'.format(config["workspace_name"])

    if VpcId is None:
        cli_logger.print("This VPC: {} has not existed. No need to delete it.".format(vpc_name))
        return

    """ Delete the VPC """
    cli_logger.print("Deleting the VPC: {}...".format(vpc_name))

    try:
        compute.networks().delete(project=project_id, network=VpcId).execute()
        cli_logger.print("Successfully deleted the VPC: {}.".format(vpc_name))
    except Exception as e:
        cli_logger.error("Failed to delete the VPC:{}. {}".format(vpc_name, str(e)))
        raise e

    return


def create_vpc(config, compute):
    project_id = config["provider"].get("project_id")
    network_body = {
        "autoCreateSubnetworks": False,
        "description": "Auto created network by cloudtik",
        "name": 'cloudtik-{}-vpc'.format(config["workspace_name"]),
        "routingConfig": {
            "routingMode": "REGIONAL"
        },
        "mtu": 1460
    }

    cli_logger.print("Creating workspace vpc on GCP...")
    # create vpc
    try:
        compute.networks().insert(project=project_id, body=network_body).execute().get("id")
        time.sleep(20)
        cli_logger.print("Successfully created workspace VPC: cloudtik-{}-vpc.".format(config["workspace_name"]))
    except Exception as e:
        cli_logger.error(
            "Failed to create workspace VPC. {}", str(e))
        raise e


def get_working_node_vpc_id(config, compute):
    ip_address = get_node_ip_address(address="8.8.8.8:53")
    project_id = config["provider"].get("project_id")
    zone = config["provider"].get("availability_zone")
    instances = compute.instances().list(project=project_id, zone=zone).execute()["items"]
    network = None
    for instance in instances:
        for networkInterface in instance.get("networkInterfaces"):
            if networkInterface.get("networkIP") == ip_address:
                network = networkInterface.get("network").split("/")[-1]
                break

    if network is None:
        cli_logger.error("Failed to get the VpcId of the working node. "
                         "Please check whether the working node is a GCP instance or not!")
        return None

    cli_logger.print("Successfully get the VpcId for working node.")
    return compute.networks().get(project=project_id, network=network).execute()["id"]


def _configure_gcp_subnets_cidr(config, compute, VpcId):
    project_id = config["provider"].get("project_id")
    region = config["provider"].get("region")
    vpc_self_link = compute.networks().get(project=project_id, network=VpcId).execute()["selfLink"]
    subnets = compute.subnetworks().list(project=project_id, region=region,
                                         filter='((network = \"{}\"))'.format(vpc_self_link)).execute().get("items", [])
    cidr_list = []

    if len(subnets) == 0:
        for i in range(0, 2):
            cidr_list.append("10.0." + str(i) + ".0/24")
    else:
        cidr_blocks = [subnet["ipCidrRange"] for subnet in subnets]
        ip = cidr_blocks[0].split("/")[0].split(".")
        for i in range(0, 256):
            tmp_cidr_block = ip[0] + "." + ip[1] + "." + str(i) + ".0/24"
            if check_cidr_conflict(tmp_cidr_block, cidr_blocks):
                cidr_list.append(tmp_cidr_block)
                cli_logger.print("Choose CIDR: {}".format(tmp_cidr_block))

            if len(cidr_list) == 2:
                break

    return cidr_list


def _delete_subnet(config, compute, isPrivate=True):
    if isPrivate:
        subnet_attribute = "private"
    else:
        subnet_attribute = "public"
    project_id = config["provider"].get("project_id")
    region = config["provider"].get("region")
    workspace_name = config["workspace_name"]
    subnetwork_name = "cloudtik-{}-{}-subnet".format(workspace_name,
                                                     subnet_attribute)

    if get_subnet(config, subnetwork_name, compute) is None:
        cli_logger.print("The {} subnet \"{}\"  is found for workspace."
                         .format(subnet_attribute, subnetwork_name))
        return

    # """ Delete custom subnet """
    cli_logger.print("Deleting {} subnet: {}...".format(subnet_attribute, subnetwork_name))
    try:
        compute.subnetworks().delete(project=project_id, region=region,
                                         subnetwork=subnetwork_name).execute()
        cli_logger.print("Successfully deleted {} subnet: {}."
                         .format(subnet_attribute, subnetwork_name))
    except Exception as e:
        cli_logger.error("Failed to delete the {} subnet: {}! {}"
                         .format(subnet_attribute, subnetwork_name, str(e)))
        raise e

    return


def _create_and_configure_subnets(config, compute, VpcId):
    project_id = config["provider"]["project_id"]
    region = config["provider"]["region"]

    cidr_list = _configure_gcp_subnets_cidr(config, compute, VpcId)
    assert len(cidr_list) == 2, "We must create 2 subnets for VPC: {}!".format(VpcId)

    subnets_attribute = ["public", "private"]
    for i in range(2):
        cli_logger.print("Creating subnet for the vpc: {} with CIDR: {}...".format(VpcId, cidr_list[i]))
        network_body = {
            "description": "Auto created {} subnet for cloudtik".format(subnets_attribute[i]),
            "enableFlowLogs": False,
            "ipCidrRange": cidr_list[i],
            "name": "cloudtik-{}-{}-subnet".format(config["workspace_name"], subnets_attribute[i]),
            "network": "projects/{}/global/networks/{}".format(project_id, VpcId),
            "stackType": "IPV4_ONLY",
            "privateIpGoogleAccess": False if subnets_attribute[i] == "public"  else True,
            "region": region
        }
        try:
            compute.subnetworks().insert(project=project_id, region=region, body=network_body).execute()
            time.sleep(10)
            cli_logger.print("Successfully created subnet: cloudtik-{}-{}-subnet.".
                             format(config["workspace_name"], subnets_attribute[i]))
        except Exception as e:
            cli_logger.error("Failed to create subnet. {}",  str(e))
            raise e

    return


def _create_router(config, compute, VpcId):
    project_id = config["provider"]["project_id"]
    region = config["provider"]["region"]
    workspace_name = config["workspace_name"]
    router_body = {
        "bgp": {
            "advertiseMode": "CUSTOM"
        },
        "description": "auto created for the workspace: cloudtik-{}-vpc".format(workspace_name),
        "name": "cloudtik-{}-private-router".format(workspace_name),
        "network": "projects/{}/global/networks/{}".format(project_id, VpcId),
        "region": "projects/{}/regions/{}".format(project_id, region)
    }
    cli_logger.print("Creating router for the private subnet: "
                     "cloudtik-{}-private-subnet...".format(workspace_name))
    try:
        compute.routers().insert(project=project_id, region=region, body=router_body).execute()
        time.sleep(20)
        cli_logger.print("Successfully created router for the private subnet: cloudtik-{}-subnet.".
                     format(config["workspace_name"]))
    except Exception as e:
        cli_logger.error("Failed to create router. {}", str(e))
        raise e

    return


def _create_nat_for_router(config, compute):
    project_id = config["provider"]["project_id"]
    region = config["provider"]["region"]
    workspace_name = config["workspace_name"]
    router = "cloudtik-{}-private-router".format(workspace_name)
    subnetwork_name = "cloudtik-{}-private-subnet".format(workspace_name)
    private_subnet = get_subnet(config, subnetwork_name, compute)
    private_subnet_selfLink = private_subnet.get("selfLink")
    nat_name = "cloutik-{}-nat".format(workspace_name)
    router_body ={
        "nats": [
            {
                "natIpAllocateOption": "AUTO_ONLY",
                "name": nat_name,
                "subnetworks": [
                    {
                        "sourceIpRangesToNat": [
                            "ALL_IP_RANGES"
                        ],
                        "name": private_subnet_selfLink
                    }
                ],
                "sourceSubnetworkIpRangesToNat": "LIST_OF_SUBNETWORKS"
            }
        ]
    }

    cli_logger.print("Creating nat-gateway \"{}\"  for private router... ".format(nat_name))
    try:
        compute.routers().patch(project=project_id, region=region, router=router, body=router_body).execute()
        cli_logger.print("Successfully created nat-gateway for the private router: {}.".
                         format(nat_name))
    except Exception as e:
        cli_logger.error("Failed to create nat-gateway. {}", str(e))
        raise e

    return


def _delete_router(config, compute):
    project_id = config["provider"]["project_id"]
    region = config["provider"]["region"]
    workspace_name = config["workspace_name"]
    router_name = "cloudtik-{}-private-router".format(workspace_name)

    if get_router(config, router_name, compute) is None:
        return

    # """ Delete custom subnet """
    cli_logger.print("Deleting the router: {}...".format(router_name))
    try:
        compute.routers().delete(project=project_id, region=region, router=router_name).execute()
        time.sleep(20)
        cli_logger.print("Successfully deleted the router: {}.".format(router_name))
    except Exception as e:
        cli_logger.error("Failed to delete the router: {}. {}".format(router_name, str(e)))
        raise e

    return


def check_firewall_exsit(config, compute, firewall_name):
    if get_firewall(config, compute, firewall_name) is None:
        cli_logger.verbose("The firewall \"{}\" doesn't exist.".format(firewall_name))
        return False
    else:
        cli_logger.verbose("The firewall \"{}\" exists.".format(firewall_name))
        return True


def get_firewall(config, compute, firewall_name):
    project_id = config["provider"]["project_id"]
    firewall = None
    cli_logger.verbose("Getting the existing firewall: {}...".format(firewall_name))
    try:
        firewall = compute.firewalls().get(project=project_id, firewall=firewall_name).execute()
        cli_logger.verbose("Successfully get the firewall: {}.".format(firewall_name))
    except Exception:
        cli_logger.verbose_error("Failed to get the firewall: {}.".format(firewall_name))
    return firewall


def create_firewall(compute, project_id, firewall_body):
    cli_logger.print("Creating firewall \"{}\"... ".format(firewall_body.get("name")))
    try:
        compute.firewalls().insert(project=project_id, body=firewall_body).execute()
        cli_logger.print("Successfully created firewall \"{}\". ".format(firewall_body.get("name")))
    except Exception as e:
        cli_logger.error("Failed to create firewall. {}", str(e))
        raise e


def enfored_create_firewall(config, compute, firewall_body):
    firewall_name = firewall_body.get("name")
    project_id = config["provider"]["project_id"]

    if not check_firewall_exsit(config, compute, firewall_name):
        create_firewall(compute, project_id, firewall_body)
    else:
        cli_logger.print("This  firewall \"{}\"  has existed, should be removed first. ".format(firewall_name))
        delete_firewall(compute, project_id, firewall_name)
        create_firewall(compute, project_id, firewall_body)


def _create_default_allow_ssh_firewall(config, compute, VpcId):
    project_id = config["provider"]["project_id"]
    workspace_name = config["workspace_name"]
    firewall_name = "cloudtik-{}-default-allow-ssh-firewall".format(workspace_name)
    firewall_body = {
        "name": firewall_name,
        "network": "projects/{}/global/networks/{}".format(project_id, VpcId),
        "allowed": [
          {
            "IPProtocol": "tcp",
            "ports": [
              "22"
            ]
          }
        ],
        "sourceRanges": [
          "0.0.0.0/0"
        ]
    }

    enfored_create_firewall(config, compute, firewall_body)


def get_subnetworks_ipCidrRange(config, compute, VpcId):
    project_id = config["provider"]["project_id"]
    subnetworks = compute.networks().get(project=project_id, network=VpcId).execute().get("subnetworks")
    subnetwork_cidrs = []
    for subnetwork in subnetworks:
        info = subnetwork.split("projects/" + project_id + "/regions/")[-1].split("/")
        subnetwork_region = info[0]
        subnetwork_name = info[-1]
        subnetwork_cidrs.append(compute.subnetworks().get(project=project_id,
                                                          region=subnetwork_region, subnetwork=subnetwork_name)
                                .execute().get("ipCidrRange"))
    return subnetwork_cidrs


def _create_default_allow_internal_firewall(config, compute, VpcId):
    project_id = config["provider"]["project_id"]
    workspace_name = config["workspace_name"]
    subnetwork_cidrs = get_subnetworks_ipCidrRange(config, compute, VpcId)
    firewall_name = "cloudtik-{}-default-allow-internal-firewall".format(workspace_name)
    firewall_body = {
        "name": firewall_name,
        "network": "projects/{}/global/networks/{}".format(project_id, VpcId),
        "allowed": [
            {
                "IPProtocol": "tcp",
                "ports": [
                    "0-65535"
                ]
            },
            {
                "IPProtocol": "udp",
                "ports": [
                    "0-65535"
                ]
            },
            {
                "IPProtocol": "icmp"
            }
        ],
        "sourceRanges": subnetwork_cidrs
    }

    enfored_create_firewall(config, compute, firewall_body)


def _create_custom_firewalls(config, compute, VpcId):
    firewall_rules = config["provider"] \
        .get("firewalls", {}) \
        .get("firewall_rules", [])

    project_id = config["provider"]["project_id"]
    workspace_name = config["workspace_name"]

    for i in range(len(firewall_rules)):
        firewall_body = {
            "name": "cloudtik-{}-custom-{}-firewall".format(workspace_name, i),
            "network": "projects/{}/global/networks/{}".format(project_id, VpcId),
            "allowed": firewall_rules[i]["allowed"],
            "sourceRanges": firewall_rules[i]["sourceRanges"]
        }
        enfored_create_firewall(config, compute, firewall_body)


def _create_firewalls(config, compute, VpcId):
    _create_default_allow_ssh_firewall(config, compute, VpcId)
    _create_default_allow_internal_firewall(config, compute, VpcId)
    _create_custom_firewalls(config, compute, VpcId)


def check_workspace_firewalls(config, compute):
    workspace_name = config["workspace_name"]
    firewall_names = ["cloudtik-{}-default-allow-internal-firewall".format(workspace_name),
                      "cloudtik-{}-default-allow-ssh-firewall".format(workspace_name)]
    custom_firewalls_num = len(config["provider"] \
        .get("firewalls", {}) \
        .get("firewall_rules", []))

    for i in range(0, custom_firewalls_num):
        firewall_names.append("cloudtik-{}-custom-{}-firewall".format(workspace_name, i))

    for firewall_name in firewall_names:
        if not check_firewall_exsit(config, compute, firewall_name):
            return False

    return True


def delete_firewall(compute, project_id, firewall_name):
    cli_logger.print("Deleting the firewall {}... ".format(firewall_name))
    try:
        compute.firewalls().delete(project=project_id, firewall=firewall_name).execute()
        cli_logger.print("Successfully delete the firewall {}.".format(firewall_name))
    except Exception as e:
        cli_logger.error(
            "Failed to delete the firewall {}. {}".format(firewall_name, str(e)))
        raise e


def _delete_firewalls(config, compute):
    project_id = config["provider"]["project_id"]
    workspace_name = config["workspace_name"]
    cloudtik_firewalls = [ firewall.get("name")
        for firewall in compute.firewalls().list(project=project_id).execute().get("items")
            if "cloudtik-{}".format(workspace_name) in firewall.get("name")]

    cli_logger.print("Deleting all the firewalls...")
    for cloudtik_firewall in cloudtik_firewalls:
        delete_firewall(compute, project_id, cloudtik_firewall)
    #Wait for all the firewalls have been deleted.
    time.sleep(20)


def get_gcp_vpcId(config, compute, use_internal_ips):
    if use_internal_ips:
        VpcId = get_working_node_vpc_id(config, compute)
    else:
        VpcId = get_workspace_vpc_id(config, compute)
    return VpcId


def delete_workspace_gcp(config):
    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])

    workspace_name = config["workspace_name"]
    use_internal_ips = config["provider"].get("use_internal_ips", False)
    VpcId = get_gcp_vpcId(config, compute, use_internal_ips)
    if VpcId is None:
        cli_logger.print("Workspace: {} doesn't exist!".format(config["workspace_name"]))
        return

    current_step = 1
    total_steps = NUM_GCP_WORKSPACE_DELETION_STEPS
    if not use_internal_ips:
        total_steps += 1

    try:

        with cli_logger.group("Deleting workspace: {}", workspace_name):
            _delete_network_resources(config, compute, current_step, total_steps)

    except Exception as e:
        cli_logger.error(
            "Failed to delete workspace {}. {}".format(workspace_name, str(e)))
        raise e

    cli_logger.print(
            "Successfully deleted workspace: {}.",
            cf.bold(workspace_name))
    return None


def _delete_network_resources(config, compute, current_step, total_steps):
    use_internal_ips = config["provider"].get("use_internal_ips", False)

    """
         Do the work - order of operation
         1.) Delete public subnet
         2.) Delete router for private subnet 
         3.) Delete private subnets
         4.) Delete firewalls
         5.) Delete vpc
    """

    # delete public subnets
    with cli_logger.group(
            "Deleting public subnet",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _delete_subnet(config, compute, isPrivate=False)

    # delete router for private subnets
    with cli_logger.group(
            "Deleting router",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _delete_router(config, compute)

    # delete private subnets
    with cli_logger.group(
            "Deleting private subnet",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _delete_subnet(config, compute, isPrivate=True)

    # delete firewalls
    with cli_logger.group(
            "Deleting firewall rules",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _delete_firewalls(config, compute)

    # delete vpc
    if not use_internal_ips:
        with cli_logger.group(
                "Deleting VPC",
                _numbered=("[]", current_step, total_steps)):
            current_step += 1
            _delete_vpc(config, compute)


def _create_vpc(config, compute):
    workspace_name = config["workspace_name"]
    use_internal_ips = config["provider"].get("use_internal_ips", False)
    if use_internal_ips:
        # No need to create new vpc
        VpcId = get_working_node_vpc_id(config, compute)
        if VpcId is None:
            cli_logger.abort("Only when the  working node is "
                             "an GCP  instance can use use_internal_ips=True.")
    else:

        # Need to create a new vpc
        if get_workspace_vpc_id(config, compute) is None:
            create_vpc(config, compute)
            VpcId = get_workspace_vpc_id(config, compute)
        else:
            cli_logger.abort("There is a existing VPC with the same name: {}, "
                             "if you want to create a new workspace with the same name, "
                             "you need to execute workspace delete first!".format(workspace_name))
    return VpcId


def _configure_network_resources(config, current_step, total_steps):
    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])

    # create vpc
    with cli_logger.group(
            "Creating VPC",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        VpcId = _create_vpc(config, compute)

    # create subnets
    with cli_logger.group(
            "Creating subnets",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _create_and_configure_subnets(config, compute, VpcId)

    # create router
    with cli_logger.group(
            "Creating router",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _create_router(config, compute, VpcId)

    # create nat-gateway for router
    with cli_logger.group(
            "Creating NAT for router",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _create_nat_for_router(config, compute)

    # create firewalls
    with cli_logger.group(
            "Creating firewall rules",
            _numbered=("[]", current_step, total_steps)):
        current_step += 1
        _create_firewalls(config, compute, VpcId)

    return config


def check_gcp_workspace_resource(config):
    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])
    use_internal_ips = config["provider"].get("use_internal_ips", False)
    workspace_name = config["workspace_name"]

    """
         Do the work - order of operation
         1.) Check VPC 
         2.) Check private subnet
         3.) Check public subnet
         4.) Check router
         5.) Check firewalls
    """
    if get_gcp_vpcId(config, compute, use_internal_ips) is None:
        return False
    if get_subnet(config, "cloudtik-{}-private-subnet".format(workspace_name), compute) is None:
        return False
    if get_subnet(config, "cloudtik-{}-public-subnet".format(workspace_name), compute) is None:
        return False
    if get_router(config, "cloudtik-{}-private-router".format(workspace_name), compute) is None:
        return False
    if check_workspace_firewalls(config, compute):
        return False
    return True


def _fix_disk_type_for_disk(zone, disk):
    # fix disk type for all disks
    initialize_params = disk.get("initializeParams")
    if initialize_params is None:
        return

    disk_type = initialize_params.get("diskType")
    if disk_type is None or "diskTypes" in disk_type:
        return

    # Fix to format: zones/zone/diskTypes/diskType
    fix_disk_type = "zones/{}/diskTypes/{}".format(zone, disk_type)
    initialize_params["diskType"] = fix_disk_type


def _fix_disk_info_for_disk(zone, disk, boot, source_image):
    if boot:
        # Need to fix source image for only boot disk
        if "initializeParams" not in disk:
            disk["initializeParams"] = {"sourceImage": source_image}
        else:
            disk["initializeParams"]["sourceImage"] = source_image

    _fix_disk_type_for_disk(zone, disk)


def _fix_disk_info_for_node(node_config, zone):
    source_image = node_config.get("sourceImage", None)
    disks = node_config.get("disks", [])
    for disk in disks:
        boot = disk.get("boot", False)
        _fix_disk_info_for_disk(zone, disk, boot, source_image)

    # Remove the sourceImage from node config
    node_config.pop("sourceImage")


def _fix_disk_info(config):
    zone = config["provider"]["availability_zone"]
    for node_type in config["available_node_types"].values():
        node_config = node_type["node_config"]
        _fix_disk_info_for_node(node_config, zone)

    return config


def bootstrap_gcp(config):
    config = copy.deepcopy(config)
    
    # Used internally to store head IAM role.
    config["head_node"] = {}

    # Check if we have any TPUs defined, and if so,
    # insert that information into the provider config
    if _has_tpus_in_node_configs(config):
        config["provider"][HAS_TPU_PROVIDER_FIELD] = True

        # We can't run autoscaling through a serviceAccount on TPUs (atm)
        if _is_head_node_a_tpu(config):
            raise RuntimeError("TPUs are not supported as head nodes.")

    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])

    config = _fix_disk_info(config)
    config = _configure_project(config, crm)
    config = _configure_iam_role(config, crm, iam)
    config = _configure_key_pair(config, compute)
    config = _configure_subnet(config, compute)

    return config


def bootstrap_gcp_from_workspace(config):
    config = copy.deepcopy(config)

    # Used internally to store head IAM role.
    config["head_node"] = {}

    # Check if we have any TPUs defined, and if so,
    # insert that information into the provider config
    if _has_tpus_in_node_configs(config):
        config["provider"][HAS_TPU_PROVIDER_FIELD] = True

        # We can't run autoscaling through a serviceAccount on TPUs (atm)
        if _is_head_node_a_tpu(config):
            raise RuntimeError("TPUs are not supported as head nodes.")

    crm, iam, compute, tpu = \
        construct_clients_from_provider_config(config["provider"])

    config = _fix_disk_info(config)
    config = _configure_iam_role(config, crm, iam)
    config = _configure_key_pair(config, compute)
    config = _configure_subnet_from_workspace(config, compute)

    return config


def _configure_project(config, crm):
    """Setup a Google Cloud Platform Project.

    Google Compute Platform organizes all the resources, such as storage
    buckets, users, and instances under projects. This is different from
    aws ec2 where everything is global.
    """
    config = copy.deepcopy(config)

    project_id = config["provider"].get("project_id")
    assert config["provider"]["project_id"] is not None, (
        "'project_id' must be set in the 'provider' section of the scaler"
        " config. Notice that the project id must be globally unique.")
    project = _get_project(project_id, crm)

    if project is None:
        #  Project not found, try creating it
        _create_project(project_id, crm)
        project = _get_project(project_id, crm)

    assert project is not None, "Failed to create project"
    assert project["lifecycleState"] == "ACTIVE", (
        "Project status needs to be ACTIVE, got {}".format(
            project["lifecycleState"]))

    config["provider"]["project_id"] = project["projectId"]

    return config


def _configure_iam_role(config, crm, iam):
    """Setup a gcp service account with IAM roles.

    Creates a gcp service acconut and binds IAM roles which allow it to control
    control storage/compute services. Specifically, the head node needs to have
    an IAM role that allows it to create further gce instances and store items
    in google cloud storage.

    TODO: Allow the name/id of the service account to be configured
    """
    config = copy.deepcopy(config)

    email = SERVICE_ACCOUNT_EMAIL_TEMPLATE.format(
        account_id=CLOUDTIK_DEFAULT_SERVICE_ACCOUNT_ID,
        project_id=config["provider"]["project_id"])
    service_account = _get_service_account(email, config, iam)

    if service_account is None:
        logger.info("_configure_iam_role: "
                    "Creating new service account {}".format(
                        CLOUDTIK_DEFAULT_SERVICE_ACCOUNT_ID))

        service_account = _create_service_account(
            CLOUDTIK_DEFAULT_SERVICE_ACCOUNT_ID, DEFAULT_SERVICE_ACCOUNT_CONFIG, config,
            iam)

    assert service_account is not None, "Failed to create service account"

    if config["provider"].get(HAS_TPU_PROVIDER_FIELD, False):
        roles = DEFAULT_SERVICE_ACCOUNT_ROLES + TPU_SERVICE_ACCOUNT_ROLES
    else:
        roles = DEFAULT_SERVICE_ACCOUNT_ROLES

    _add_iam_policy_binding(service_account, roles, crm)

    config["head_node"]["serviceAccounts"] = [{
        "email": service_account["email"],
        # NOTE: The amount of access is determined by the scope + IAM
        # role of the service account. Even if the cloud-platform scope
        # gives (scope) access to the whole cloud-platform, the service
        # account is limited by the IAM rights specified below.
        "scopes": ["https://www.googleapis.com/auth/cloud-platform"]
    }]

    return config


def _configure_key_pair(config, compute):
    """Configure SSH access, using an existing key pair if possible.

    Creates a project-wide ssh key that can be used to access all the instances
    unless explicitly prohibited by instance config.

    The ssh-keys created are of format:

      [USERNAME]:ssh-rsa [KEY_VALUE] [USERNAME]

    where:

      [USERNAME] is the user for the SSH key, specified in the config.
      [KEY_VALUE] is the public SSH key value.
    """
    config = copy.deepcopy(config)

    if "ssh_private_key" in config["auth"]:
        return config

    ssh_user = config["auth"]["ssh_user"]

    project = compute.projects().get(
        project=config["provider"]["project_id"]).execute()

    # Key pairs associated with project meta data. The key pairs are general,
    # and not just ssh keys.
    ssh_keys_str = next(
        (item for item in project["commonInstanceMetadata"].get("items", [])
         if item["key"] == "ssh-keys"), {}).get("value", "")

    ssh_keys = ssh_keys_str.split("\n") if ssh_keys_str else []

    # Try a few times to get or create a good key pair.
    key_found = False
    for i in range(10):
        key_name = key_pair_name(i, config["provider"]["region"],
                                 config["provider"]["project_id"], ssh_user)
        public_key_path, private_key_path = key_pair_paths(key_name)

        for ssh_key in ssh_keys:
            key_parts = ssh_key.split(" ")
            if len(key_parts) != 3:
                continue

            if key_parts[2] == ssh_user and os.path.exists(private_key_path):
                # Found a key
                key_found = True
                break

        # Writing the new ssh key to the filesystem fails if the ~/.ssh
        # directory doesn't already exist.
        os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)

        # Create a key since it doesn't exist locally or in GCP
        if not key_found and not os.path.exists(private_key_path):
            logger.info("_configure_key_pair: "
                        "Creating new key pair {}".format(key_name))
            public_key, private_key = generate_rsa_key_pair()

            _create_project_ssh_key_pair(project, public_key, ssh_user,
                                         compute)

            # Create the directory if it doesn't exists
            private_key_dir = os.path.dirname(private_key_path)
            os.makedirs(private_key_dir, exist_ok=True)

            # We need to make sure to _create_ the file with the right
            # permissions. In order to do that we need to change the default
            # os.open behavior to include the mode we want.
            with open(
                    private_key_path,
                    "w",
                    opener=partial(os.open, mode=0o600),
            ) as f:
                f.write(private_key)

            with open(public_key_path, "w") as f:
                f.write(public_key)

            key_found = True

            break

        if key_found:
            break

    assert key_found, "SSH keypair for user {} not found for {}".format(
        ssh_user, private_key_path)
    assert os.path.exists(private_key_path), (
        "Private key file {} not found for user {}"
        "".format(private_key_path, ssh_user))

    cli_logger.print("Private key not specified in config, using "
                     "{}".format(private_key_path))

    config["auth"]["ssh_private_key"] = private_key_path

    return config


def _configure_subnet(config, compute):
    """Pick a reasonable subnet if not specified by the config."""
    config = copy.deepcopy(config)

    node_configs = [
        node_type["node_config"]
        for node_type in config["available_node_types"].values()
    ]
    # Rationale: avoid subnet lookup if the network is already
    # completely manually configured

    # networkInterfaces is compute, networkConfig is TPU
    if all("networkInterfaces" in node_config or "networkConfig" in node_config
           for node_config in node_configs):
        return config

    subnets = _list_subnets(config, compute)

    if not subnets:
        raise NotImplementedError("Should be able to create subnet.")

    # TODO: make sure that we have usable subnet. Maybe call
    # compute.subnetworks().listUsable? For some reason it didn't
    # work out-of-the-box
    default_subnet = subnets[0]

    default_interfaces = [{
        "subnetwork": default_subnet["selfLink"],
        "accessConfigs": [{
            "name": "External NAT",
            "type": "ONE_TO_ONE_NAT",
        }],
    }]

    for node_config in node_configs:
        # The not applicable key will be removed during node creation

        # compute
        if "networkInterfaces" not in node_config:
            node_config["networkInterfaces"] = copy.deepcopy(
                default_interfaces)
        # TPU
        if "networkConfig" not in node_config:
            node_config["networkConfig"] = copy.deepcopy(default_interfaces)[0]
            node_config["networkConfig"].pop("accessConfigs")

    return config


def _configure_subnet_from_workspace(config, compute):
    workspace_name = config["workspace_name"]
    use_internal_ips = config["provider"].get("use_internal_ips", False)

    """Pick a reasonable subnet if not specified by the config."""
    config = copy.deepcopy(config)

    # Rationale: avoid subnet lookup if the network is already
    # completely manually configured

    # networkInterfaces is compute, networkConfig is TPU
    public_subnet = get_subnet(config, "cloudtik-{}-public-subnet".format(workspace_name), compute)
    private_subnet = get_subnet(config, "cloudtik-{}-private-subnet".format(workspace_name), compute)

    public_interfaces = [{
        "subnetwork": public_subnet["selfLink"],
        "accessConfigs": [{
            "name": "External NAT",
            "type": "ONE_TO_ONE_NAT",
        }],
    }]

    private_interfaces = [{
        "subnetwork": private_subnet["selfLink"],
    }]

    for key, node_type in config["available_node_types"].items():
        node_config = node_type["node_config"]
        if key == config["head_node_type"]:
            if use_internal_ips:
                # compute
                node_config["networkInterfaces"] = copy.deepcopy(private_interfaces)
                # TPU
                node_config["networkConfig"] = copy.deepcopy(private_interfaces)[0]
            else:
                # compute
                node_config["networkInterfaces"] = copy.deepcopy(public_interfaces)
                # TPU
                node_config["networkConfig"] = copy.deepcopy(public_interfaces)[0]
                node_config["networkConfig"].pop("accessConfigs")
        else:
            # compute
            node_config["networkInterfaces"] = copy.deepcopy(private_interfaces)
            # TPU
            node_config["networkConfig"] = copy.deepcopy(private_interfaces)[0]

    return config


def _list_subnets(config, compute):
    response = compute.subnetworks().list(
        project=config["provider"]["project_id"],
        region=config["provider"]["region"]).execute()

    return response["items"]


def get_subnet(config, subnetwork_name, compute):
    cli_logger.verbose("Getting the existing subnet: {}.".format(subnetwork_name))
    try:
        subnet = compute.subnetworks().get(
            project=config["provider"]["project_id"],
            region=config["provider"]["region"],
            subnetwork=subnetwork_name,
        ).execute()
        cli_logger.verbose("Successfully get the subnet: {}.".format(subnetwork_name))
        return subnet
    except Exception:
        cli_logger.verbose_error("Failed to get the subnet: {}.".format(subnetwork_name))
        return None


def get_router(config, router_name, compute):
    cli_logger.verbose("Getting the existing router: {}.".format(router_name))
    try:
        router = compute.routers().get(
            project=config["provider"]["project_id"],
            region=config["provider"]["region"],
            router=router_name,
        ).execute()
        cli_logger.verbose("Successfully get the router: {}.".format(router_name))
        return router
    except Exception:
        cli_logger.verbose_error("Failed to get the router: {}.".format(router_name))
        return None


def _get_project(project_id, crm):
    try:
        project = crm.projects().get(projectId=project_id).execute()
    except errors.HttpError as e:
        if e.resp.status != 403:
            raise
        project = None

    return project


def _create_project(project_id, crm):
    operation = crm.projects().create(body={
        "projectId": project_id,
        "name": project_id
    }).execute()

    result = wait_for_crm_operation(operation, crm)

    return result


def _get_service_account(account, config, iam):
    project_id = config["provider"]["project_id"]
    full_name = ("projects/{project_id}/serviceAccounts/{account}"
                 "".format(project_id=project_id, account=account))
    try:
        service_account = iam.projects().serviceAccounts().get(
            name=full_name).execute()
    except errors.HttpError as e:
        if e.resp.status != 404:
            raise
        service_account = None

    return service_account


def _create_service_account(account_id, account_config, config, iam):
    project_id = config["provider"]["project_id"]

    service_account = iam.projects().serviceAccounts().create(
        name="projects/{project_id}".format(project_id=project_id),
        body={
            "accountId": account_id,
            "serviceAccount": account_config,
        }).execute()

    return service_account


def _add_iam_policy_binding(service_account, roles, crm):
    """Add new IAM roles for the service account."""
    project_id = service_account["projectId"]
    email = service_account["email"]
    member_id = "serviceAccount:" + email

    policy = crm.projects().getIamPolicy(
        resource=project_id, body={}).execute()

    already_configured = True
    for role in roles:
        role_exists = False
        for binding in policy["bindings"]:
            if binding["role"] == role:
                if member_id not in binding["members"]:
                    binding["members"].append(member_id)
                    already_configured = False
                role_exists = True

        if not role_exists:
            already_configured = False
            policy["bindings"].append({
                "members": [member_id],
                "role": role,
            })

    if already_configured:
        # In some managed environments, an admin needs to grant the
        # roles, so only call setIamPolicy if needed.
        return

    result = crm.projects().setIamPolicy(
        resource=project_id, body={
            "policy": policy,
        }).execute()

    return result


def _create_project_ssh_key_pair(project, public_key, ssh_user, compute):
    """Inserts an ssh-key into project commonInstanceMetadata"""

    key_parts = public_key.split(" ")

    # Sanity checks to make sure that the generated key matches expectation
    assert len(key_parts) == 2, key_parts
    assert key_parts[0] == "ssh-rsa", key_parts

    new_ssh_meta = "{ssh_user}:ssh-rsa {key_value} {ssh_user}".format(
        ssh_user=ssh_user, key_value=key_parts[1])

    common_instance_metadata = project["commonInstanceMetadata"]
    items = common_instance_metadata.get("items", [])

    ssh_keys_i = next(
        (i for i, item in enumerate(items) if item["key"] == "ssh-keys"), None)

    if ssh_keys_i is None:
        items.append({"key": "ssh-keys", "value": new_ssh_meta})
    else:
        ssh_keys = items[ssh_keys_i]
        ssh_keys["value"] += "\n" + new_ssh_meta
        items[ssh_keys_i] = ssh_keys

    common_instance_metadata["items"] = items

    operation = compute.projects().setCommonInstanceMetadata(
        project=project["name"], body=common_instance_metadata).execute()

    response = wait_for_compute_global_operation(project["name"], operation,
                                                 compute)

    return response
