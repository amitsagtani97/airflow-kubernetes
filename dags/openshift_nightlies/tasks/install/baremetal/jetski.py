from os import environ

from openshift_nightlies.util import var_loader, executor, constants
from openshift_nightlies.tasks.install.baremetal import webfuse
from openshift_nightlies.tasks.install.openshift import AbstractOpenshiftInstaller
from openshift_nightlies.models.release import BaremetalRelease
from openshift_nightlies.models.dag_config import DagConfig

import json

from airflow.operators.bash import BashOperator
from airflow.models import Variable
from kubernetes.client import models as k8s

# Defines Tasks for installation of Openshift Clusters
class BaremetalOpenshiftInstaller(AbstractOpenshiftInstaller):
    def __init__(self, dag, config: DagConfig, release: BaremetalRelease):
        self.baremetal_install_secrets = var_loader.get_secret(
            f"baremetal_openshift_install_config", deserialize_json=True)
        super().__init__(dag, config, release)

    def get_install_task(self):
        webfuse_installer = self._get_webfuse_installer()
        install_cluster = self._get_task(operation="install")
        deploy_webfuse = webfuse_installer.get_deploy_app_task()

        install_cluster >> deploy_webfuse

        return install_cluster

    def get_scaleup_task(self):
        return self._get_task(operation="scaleup")

    def _get_webfuse_installer(self):
        return webfuse.BaremetalWebfuseInstaller(self.dag, self.dag_config, self.release)

    # Create Airflow Task for Install/Cleanup steps
    def _get_task(self, operation="install", trigger_rule="all_success"):
        bash_script = ""

        # Merge all variables, prioritizing Airflow Secrets over git based vars
        config = {
            **self.vars,
            **self.baremetal_install_secrets,
            **{ "es_server": var_loader.get_secret('elasticsearch') }
        }
        
        config['pullsecret'] = json.dumps(config['openshift_install_pull_secret'])
        config['version'] = config['openshift_release']
        config['build'] = self.release.build
        
        # Required Environment Variables for Install script
        env = {
            "SSHKEY_TOKEN": config['sshkey_token'],
            "OPENSHIFT_WORKER_COUNT": config['openshift_worker_count'],
            "BAREMETAL_NETWORK_CIDR": config['baremetal_network_cidr'],
            "BAREMETAL_NETWORK_VLAN": config['baremetal_network_vlan'],
            "OPENSHIFT_BASE_DOMAIN": config['openshift_base_domain'],
            "KUBECONFIG_PATH": config['kubeconfig_path'],
            **self._insert_kube_env()
        }

        if operation == "install":
            config['worker_count'] = config['openshift_worker_count']
            bash_script = f"{constants.root_dag_dir}/scripts/install/baremetal_install.sh"
        else:
            config['worker_count'] = config['openshift_worker_scaleup_target']
            bash_script = f"{constants.root_dag_dir}/scripts/install/baremetal_scaleup.sh"

        # Dump all vars to json file for Ansible to pick up
        with open(f"/tmp/{self.release_name}-{operation}-task.json", 'w') as json_file:
            json.dump(config, json_file, sort_keys=True, indent=4)

        return BashOperator(
            task_id=f"{operation}-cluster",
            depends_on_past=False,
            bash_command=f"{bash_script} -p {self.release.platform} -v {self.release.version} -j /tmp/{self.release_name}-{operation}-task.json -o {operation} ",
            retries=3,
            dag=self.dag,
            trigger_rule=trigger_rule,
            executor_config=self.exec_config,
            env=env
        )
