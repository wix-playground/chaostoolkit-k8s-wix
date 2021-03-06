# -*- coding: utf-8 -*-
"""
Chaostoolkit custom actions/probes lib from Wix.com
"""
import json
import os
import os.path
from typing import List
import requests
from requests.exceptions import HTTPError

from chaoslib.discovery.discover import discover_actions, discover_probes, \
    initialize_discovery_result
from chaoslib.exceptions import DiscoveryFailed
from chaoslib.types import Discovery, DiscoveredActivities, \
    DiscoveredSystemInfo, Secrets
from kubernetes import client, config
from logzero import logger
import boto3

__all__ = ["create_k8s_api_client",
           "create_aws_client", "discover", "__version__", "get_slack_config"]
__version__ = '1.4.8'


def has_local_config_file():
    # due to https://github.com/kubernetes-client/python/issues/525
    # we disable kube/conf auth
    return False
    # config_path = os.path.expanduser(
    #     os.environ.get('KUBECONFIG', '~/.kube/config'))
    # return os.path.exists(config_path)


def get_kube_secret_from_production(target_url, token):
    headers = {'Authorization': 'Token ' + token,
               'Content-Type': 'application/json'}
    retval = None
    try:
        response = requests.get(target_url, headers=headers)
        retval = json.loads(response.content)

        response.raise_for_status()

    except HTTPError as http_err:
        # print(f'HTTP error occurred: {http_err}')  # Python 3.6
        pass
    except Exception as err:
        # print(f'Other error occurred: {err}')  # Python 3.6
        pass
    return retval


def get_aws_credentials(secrets):
    env = os.environ
    secrets = secrets or {}

    def lookup(k: str, d: str = None) -> str:
        return secrets.get(k, env.get(k, d))

    prod_vault_url = lookup("NASA_SECRETS_URL", "undefined")
    target_url = os.path.join(prod_vault_url, 'aws')
    token = lookup("NASA_TOKEN", "undefined")
    aws_creds = get_kube_secret_from_production(target_url, token)
    return aws_creds


def create_aws_client(secrets, resource):
    aws_creds = get_aws_credentials(secrets)
    if aws_creds is not None:
        client = boto3.client(resource,
                              aws_access_key_id=aws_creds['aws_access_key_id'],
                              aws_secret_access_key=aws_creds['aws_secret_access_key'])
    else:
        client = boto3.client(resource)

    return client


def create_aws_resource(secrets, resource):
    aws_creds = get_aws_credentials(secrets)
    if aws_creds is not None:
        client = boto3.resource(resource,
                                aws_access_key_id=aws_creds['aws_access_key_id'],
                                aws_secret_access_key=aws_creds['aws_secret_access_key'])
    else:
        client = boto3.resource(resource)

    return client


def create_k8s_api_client(secrets: Secrets = None) -> client.ApiClient:
    """
    Create a Kubernetes client from:

    1. From a local configuration file if it exists (`~/.kube/config`). You
       can specify which context you want to use as well through the
       `KUBERNETES_CONTEXT` key in the environment or in the `secrets` object.
    2. From the cluster configuration if executed from a Kubernetes pod and
       the CHAOSTOOLKIT_IN_POD is set to `"true"`.
    3. From a mix of the following environment keys:

        * KUBERNETES_HOST: Kubernetes API address

        You can authenticate with a token via:
        * KUBERNETES_API_KEY: the API key to authenticate with
        * KUBERNETES_API_KEY_PREFIX: the key kind, if not set, defaults to
          "Bearer"

        Or via a username/password:
        * KUBERNETES_USERNAME
        * KUBERNETES_PASSWORD

        Or via SSL:
        * KUBERNETES_CERT_FILE
        * KUBERNETES_KEY_FILE

        Finally, you may disable SSL verification against HTTPS endpoints:
        * KUBERNETES_VERIFY_SSL: should we verify the SSL (unset means no)
        * KUBERNETES_CA_CERT_FILE: path the CA certificate when verification is
          expected

        You may pass a secrets dictionary, in which case, values will be looked
        there before the environ.
    """
    env = os.environ
    secrets = secrets or {}

    def lookup(k: str, d: str = None) -> str:
        return secrets.get(k, env.get(k, d))

    dc = lookup("KUBERNETES_CONTEXT", "undefined")
    prod_secrets = None
    if dc is not "undefined":
        prod_vault_url = lookup("NASA_SECRETS_URL", "undefined")
        target_url = os.path.join(prod_vault_url, dc)
        token = lookup("NASA_TOKEN", "undefined")
        prod_secrets = get_kube_secret_from_production(target_url, token)

    if prod_secrets is not None:
        configuration = client.Configuration()
        configuration.debug = False
        configuration.host = prod_secrets['url']
        configuration.verify_ssl = False
#       configuration.cert_file = nasa_secrets['cert']
        configuration.api_key['authorization'] = prod_secrets['token']
        configuration.api_key_prefix['authorization'] = "Bearer"
    elif has_local_config_file():
        context = lookup("KUBERNETES_CONTEXT")
        logger.debug("Using Kubernetes context: {}".format(
            context or "default"))
        return config.new_client_from_config(context=context)
    elif env.get("CHAOSTOOLKIT_IN_POD") == "true":
        config.load_incluster_config()
        return client.ApiClient()
    else:
        configuration = client.Configuration()
        configuration.debug = False
        configuration.host = lookup("KUBERNETES_HOST", "http://localhost")
        configuration.verify_ssl = lookup(
            "KUBERNETES_VERIFY_SSL", False) is not False
        configuration.cert_file = lookup("KUBERNETES_CA_CERT_FILE")

        if "KUBERNETES_API_KEY" in env or "KUBERNETES_API_KEY" in secrets:
            configuration.api_key['authorization'] = lookup(
                "KUBERNETES_API_KEY")
            configuration.api_key_prefix['authorization'] = lookup(
                "KUBERNETES_API_KEY_PREFIX", "Bearer")
        elif "KUBERNETES_CERT_FILE" in env or \
                "KUBERNETES_CERT_FILE" in secrets:
            configuration.cert_file = lookup("KUBERNETES_CERT_FILE")
            configuration.key_file = lookup("KUBERNETES_KEY_FILE")
        elif "KUBERNETES_USERNAME" in env or "KUBERNETES_USERNAME" in secrets:
            configuration.username = lookup("KUBERNETES_USERNAME")
            configuration.password = lookup("KUBERNETES_PASSWORD", "")

    return client.ApiClient(configuration)


def discover(discover_system: bool = True) -> Discovery:
    """
    Discover Kubernetes capabilities offered by this extension.
    """
    logger.info("Discovering capabilities from chaostoolkit-k8s-wix")

    discovery = initialize_discovery_result(
        "chaostoolkit-k8s-wix", __version__, "kubernetes")
    discovery["activities"].extend(load_exported_activities())
    return discovery


###############################################################################
# Private functions
###############################################################################
def load_exported_activities() -> List[DiscoveredActivities]:
    """
    Extract metadata from actions and probes exposed by this extension.
    """
    activities = []
    activities.extend(discover_actions("chaosk8s_wix.actions"))
    activities.extend(discover_probes("chaosk8s_wix.probes"))
    activities.extend(discover_actions("chaosk8s_wix.pod.actions"))
    activities.extend(discover_probes("chaosk8s_wix.pod.probes"))
    activities.extend(discover_actions("chaosk8s_wix.node.actions"))
    activities.extend(discover_probes("chaosk8s_wix.node.probes"))
    activities.extend(discover_actions("chaosk8s_wix.aws.actions"))
    activities.extend(discover_probes("chaosk8s_wix.consul.probes"))
    activities.extend(discover_actions("chaosk8s_wix.consul.actions"))
    activities.extend(discover_probes("chaosk8s_wix.grafana.probes"))
    return activities
