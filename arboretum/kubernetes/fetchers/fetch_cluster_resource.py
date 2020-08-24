# -*- mode:python; coding:utf-8 -*-
# Copyright (c) 2020 IBM Corp. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Cluster resource fetcher for various types of clusters."""

import json
import subprocess

from arboretum.common.utils import mask_secrets

from compliance.evidence import evidences, store_raw_evidence
from compliance.fetch import ComplianceFetcher


class ClusterResourceFetcher(ComplianceFetcher):
    """Fetch the resources of clusters."""

    RESOURCE_TYPE_DEFAULT = ['node', 'pod', 'configmap']

    @classmethod
    def setUpClass(cls):
        """Initialize the fetcher object with configuration settings."""
        cls.logger = cls.locker.logger.getChild(
            'kubernetes.cluster_resource_fetcher'
        )
        return cls

    @store_raw_evidence('kubernetes/cluster_resource.json')
    def fetch_cluster_resource(self):
        """Fetch cluster resources of listed clusters."""
        cluster_list_types = self.config.get(
            'org.kubernetes.cluster_resource.cluster_list_types'
        )

        resources = {}
        for cltype in cluster_list_types:
            try:
                if cltype == 'kubernetes':
                    resources['kubernetes'] = self._fetch_bom_resource()
                elif cltype == 'ibm_cloud':
                    resources['ibm_cloud'] = self._fetch_ibm_cloud_resource()
                else:
                    self.logger.error(
                        'Specified cluster list type "%s" is not supported',
                        cltype
                    )
            except Exception as e:
                self.logger.error(
                    'Failed to fetch resources for cluster list "%s": %s',
                    cltype,
                    str(e)
                )
        return json.dumps(resources)

    def _fetch_bom_resource(self):
        resource_types = self.config.get(
            'org.kubernetes.cluster_resource.target_resource_types',
            ClusterResourceFetcher.RESOURCE_TYPE_DEFAULT
        )

        bom = {}
        with evidences(self.locker, 'raw/kubernetes/cluster_list.json') as ev:
            bom = json.loads(ev.content)

        resources = {}
        for c in bom:
            cluster_resources = []
            for r in resource_types:
                args = [
                    'kubectl',
                    '--kubeconfig',
                    c['kubeconfig'],
                    'get',
                    r,
                    '-A',
                    '-o',
                    'json'
                ]
                cp = self._run_command(args)
                out = cp.stdout
                cluster_resources.extend(json.loads(out)['items'])
            resources[c['account']] = [
                {
                    'name': c['name'], 'resources': cluster_resources
                }
            ]
        return resources

    def _fetch_ibm_cloud_resource(self):
        resource_types = self.config.get(
            'org.ibm_cloud.cluster_resource.target_resource_types',
            ClusterResourceFetcher.RESOURCE_TYPE_DEFAULT
        )
        cluster_list = {}
        with evidences(self.locker, 'raw/ibm_cloud/cluster_list.json') as ev:
            cluster_list = json.loads(ev.content)

        resources = {}
        for account in cluster_list:
            api_key = getattr(
                self.config.creds['ibm_cloud'], f'{account}_api_key'
            )
            try:
                self._run_command(
                    ['ibmcloud', 'login', '--no-region', '--apikey', api_key]
                )
            except subprocess.CalledProcessError as e:
                self.logger.error(
                    'Failed to login with account %s: %s',
                    account,
                    mask_secrets(str(e), [api_key])
                )
                continue
            try:
                resources[account] = []
                for cluster in cluster_list[account]:
                    # get configuration to access the target cluster
                    args = [
                        'ibmcloud',
                        'cs',
                        'cluster',
                        'config',
                        '-s',
                        '-c',
                        cluster['name']
                    ]
                    try:
                        self._run_command(args)
                    except subprocess.CalledProcessError as e:
                        if e.returncode == 2:  # RC: 2 == no plugin
                            self.logger.warning(
                                'Kubernetes service plugin missing.  '
                                'Attempting to install plugin...'
                            )
                            self._run_command(
                                [
                                    'ibmcloud',
                                    'plugin',
                                    'install',
                                    'kubernetes-service'
                                ]
                            )
                            self._run_command(args)
                        else:
                            raise
                    # login using "oc" command if the target is openshift
                    if cluster['type'] == 'openshift':
                        try:
                            self._run_command(
                                ['oc', 'login', '-u', 'apikey', '-p', api_key]
                            )
                        except subprocess.CalledProcessError as e:
                            self.logger.error(
                                'Failed to login an OpenShift cluster with '
                                'account %s: %s',
                                account,
                                mask_secrets(str(e), [api_key])
                            )
                            continue
                    # get resources
                    resource_list = []
                    for resource in resource_types:
                        try:
                            cp = self._run_command(
                                [
                                    'kubectl',
                                    'get',
                                    resource,
                                    '-A',
                                    '-o',
                                    'json'
                                ]
                            )
                            output = cp.stdout
                            resource_list.extend(json.loads(output)['items'])
                        except RuntimeError:
                            self.logger.warning(
                                'Failed to get %s resource in cluster %s',
                                resource,
                                cluster['name']
                            )
                    cluster['resources'] = resource_list
                    resources[account].append(cluster)
            finally:
                self._run_command(['ibmcloud', 'logout'])

        return resources

    def _run_command(self, args):
        return subprocess.run(
            args,
            text=True,
            timeout=30,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            shell=False
        )