# -*- coding: utf-8 -*-
from datetime import datetime
import json
import os.path
from typing import Dict, Union
import urllib3

from chaoslib.exceptions import FailedActivity
from chaoslib.types import MicroservicesStatus, Secrets
import dateparser
from logzero import logger
from kubernetes import client, watch
import yaml

from chaosk8s_wix import __version__, create_k8s_api_client
from chaosk8s_wix.pod.probes import read_pod_logs


__all__ = ["all_microservices_healthy", "microservice_available_and_healthy",
           "microservice_is_not_available", "service_endpoint_is_initialized",
           "deployment_is_not_fully_available", "read_microservices_logs", "all_pods_in_all_ns_are_ok"]


def all_microservices_healthy(ns: str = "default",
                              secrets: Secrets = None) -> MicroservicesStatus:
    """
    Check all microservices in the system are running and available.

    Raises :exc:`chaoslib.exceptions.FailedActivity` when the state is not
    as expected.
    """
    api = create_k8s_api_client(secrets)
    not_ready = []
    failed = []

    v1 = client.CoreV1Api(api)
    ret = v1.list_namespaced_pod(namespace=ns)
    for p in ret.items:
        phase = p.status.phase
        if phase == "Failed":
            failed.append(p)
        elif phase != "Running":
            not_ready.append(p)

    logger.debug("Found {d} failed and {n} not ready pods".format(
        d=len(failed), n=len(not_ready)))

    # we probably should list them in the message
    if failed or not_ready:
        raise FailedActivity("the system is unhealthy")

    return True


def microservice_available_and_healthy(
        name: str, ns: str = "default",
        label_selector: str = "name in ({name})",
        secrets: Secrets = None) -> Union[bool, None]:
    """
    Lookup a deployment by `name` in the namespace `ns`.

    The selected resources are matched by the given `label_selector`.

    Raises :exc:`chaoslib.exceptions.FailedActivity` when the state is not
    as expected.
    """
    label_selector = label_selector.format(name=name)
    api = create_k8s_api_client(secrets)

    v1 = client.AppsV1beta1Api(api)
    ret = v1.list_namespaced_deployment(ns, label_selector=label_selector)

    logger.debug("Found {d} deployments named '{n}'".format(
        d=len(ret.items), n=name))

    if not ret.items:
        raise FailedActivity(
            "microservice '{name}' was not found".format(name=name))

    for d in ret.items:
        logger.debug("Deployment has '{s}' available replicas".format(
            s=d.status.available_replicas))

        if d.status.available_replicas != d.spec.replicas:
            raise FailedActivity(
                "microservice '{name}' is not healthy".format(name=name))

    return True


def microservice_is_not_available(name: str, ns: str = "default",
                                  label_selector: str = "name in ({name})",
                                  secrets: Secrets = None) -> bool:
    """
    Lookup pods with a `name` label set to the given `name` in the specified
    `ns`.

    Raises :exc:`chaoslib.exceptions.FailedActivity` when one of the pods
    with the specified `name` is in the `"Running"` phase.
    """
    label_selector = label_selector.format(name=name)
    api = create_k8s_api_client(secrets)

    v1 = client.CoreV1Api(api)
    ret = v1.list_namespaced_pod(ns, label_selector=label_selector)

    logger.debug("Found {d} pod named '{n}'".format(
        d=len(ret.items), n=name))

    for p in ret.items:
        phase = p.status.phase
        logger.debug("Pod '{p}' has status '{s}'".format(
            p=p.metadata.name, s=phase))
        if phase == "Running":
            raise FailedActivity(
                "microservice '{name}' is actually running".format(name=name))

    return True


def service_endpoint_is_initialized(name: str, ns: str = "default",
                                    label_selector: str = "name in ({name})",
                                    secrets: Secrets = None):
    """
    Lookup a service endpoint by its name and raises :exc:`FailedProbe` when
    the service was not found or not initialized.
    """
    label_selector = label_selector.format(name=name)
    api = create_k8s_api_client(secrets)

    v1 = client.CoreV1Api(api)
    ret = v1.list_namespaced_service(ns, label_selector=label_selector)

    logger.debug("Found {d} services named '{n}'".format(
        d=len(ret.items), n=name))

    if not ret.items:
        raise FailedActivity(
            "service '{name}' is not initialized".format(name=name))

    return True


def deployment_is_not_fully_available(name: str, ns: str = "default",
                                      label_selector: str = "name in ({name})",
                                      timeout: int = 30,
                                      secrets: Secrets = None):
    """
    Wait until the deployment gets into an intermediate state where not all
    expected replicas are available. Once this state is reached, return `True`.
    If the state is not reached after `timeout` seconds, a
    :exc:`chaoslib.exceptions.FailedActivity` exception is raised.
    """
    label_selector = label_selector.format(name=name)
    api = create_k8s_api_client(secrets)
    v1 = client.AppsV1beta1Api(api)
    w = watch.Watch()
    timeout = int(timeout)

    try:
        logger.debug("Watching events for {t}s".format(t=timeout))
        for event in w.stream(v1.list_namespaced_deployment, namespace=ns,
                              label_selector=label_selector,
                              _request_timeout=timeout):
            deployment = event['object']
            status = deployment.status
            spec = deployment.spec

            logger.debug(
                "Deployment '{p}' {t}: "
                "Ready Replicas {r} - "
                "Unavailable Replicas {u} - "
                "Desired Replicas {a}".format(
                    p=deployment.metadata.name, t=event["type"],
                    r=status.ready_replicas,
                    a=spec.replicas,
                    u=status.unavailable_replicas))

            if status.ready_replicas != spec.replicas:
                w.stop()
                return True

    except urllib3.exceptions.ReadTimeoutError:
        logger.debug("Timed out!")
        raise FailedActivity(
            "microservice '{name}' failed to stop running within {t}s".format(
                name=name, t=timeout))


def all_pods_in_all_ns_are_ok(ns_ignore_list: []=None, secrets: Secrets = None):
    api = create_k8s_api_client(secrets)
    v1 = client.CoreV1Api(api)
    pods = v1.list_pod_for_all_namespaces(watch=False)
    retval = True
    for i in pods.items:
        if i.status.container_statuses is not None:
            for status in i.status.container_statuses:
                if status.state.running is None:
                    if i.metadata.namespace not in ns_ignore_list:
                        logger.info("%s\t%s\t%s \t%s is not good" % (
                            i.status.host_ip, i.metadata.namespace, i.metadata.name, i.status.container_statuses[0].state))
                        retval = False
                    else:
                        logger.info("%s\t%s\t%s \t%s is not good but IGNORED by back list" % (
                            i.status.host_ip, i.metadata.namespace, i.metadata.name, i.status.container_statuses[0].state))
    return retval



# moved to pod/probes.py
read_microservices_logs = read_pod_logs
