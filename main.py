import os
import signal
import time
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
    )

class AutoDockerImagePull():
    def __init__(self, proxy_url: str, no_proxy_domain: list | None = None, kube_fledged_namespace: str="kube-fledged", allow_del_pod=True, log_level="INFO", image_check_interval:int=10) -> None:
        self.proxy_url = proxy_url
        self.no_proxy_domains = set(no_proxy_domain) if no_proxy_domain else set()
        self.kube_fledged_namespace = kube_fledged_namespace
        self.allow_del_pod = allow_del_pod
        self.image_check_interval = image_check_interval
        self.logger  = logging.getLogger(__name__)
        self.logger.setLevel(log_level)
        self.nodes = set()
        
        signal.signal(signal.SIGINT, self.clean)
        signal.signal(signal.SIGTERM, self.clean)

    def get_image_pull_state_info(self, pod):
        """
        检查 Pod 是否在镜像拉取阶段
        """
        statuses = (pod.status.init_container_statuses if pod.status.init_container_statuses else []) + (pod.status.container_statuses if pod.status.container_statuses else [])
        for container_status in statuses:
            if container_status.state.waiting:
                if container_status.state.waiting.reason in ["ImagePullBackOff", "ErrImagePull"]:
                    return container_status.image, container_status.state.waiting.reason
        return None, None

    def get_pod_node(self, pod):
        """
        获取 Pod 调度到的节点
        """
        return pod.spec.node_name

    def create_image_cache(self, node: str, data: list[str]):
        """
        创建 ImageCache 资源
        """
        name = f"imagecache-node-{node}"
        images = []
        # for k, v in items.items():
        images.append({
            "images": data,
            "nodeSelector": {
                "kubernetes.io/hostname": node
            }
        })
        image_cache = {
            "apiVersion": "kubefledged.io/v1alpha2",
            "kind": "ImageCache",
            "metadata": {
                "name": name,
                "namespace": "kube-fledged",
                "labels": {
                    "app": "kubefledged",
                    "kubefledged": "imagecache"
                }
            },
            "spec": {
                "cacheSpec": images,
                "imagePullSecrets": []
            }
        }
        return image_cache

    def apply_image_cache(self, image_cache):
        """
        应用 ImageCache 资源
        """
        name = image_cache["metadata"]["name"]
        namespace = image_cache["metadata"]["namespace"]

        api_instance = client.CustomObjectsApi()
        # 如果存在就更新
    # 尝试获取现有的 ImageCache 资源
        try:
            # 如果存在，更新 ImageCache 资源
            api_response = api_instance.patch_namespaced_custom_object(
                group="kubefledged.io",
                version="v1alpha2",
                namespace=namespace,
                plural="imagecaches",
                name=name,
                body=image_cache
            )
            self.logger.info(f"ImageCache updated: {api_response['metadata']['name']}")
        except ApiException as e:
            if e.status == 404:
                api_response = api_instance.create_namespaced_custom_object(
                    group="kubefledged.io",
                    version="v1alpha2",
                    namespace="kube-fledged",
                    plural="imagecaches",
                    body=image_cache
                )
                self.logger.info(f"ImageCache created: {api_response['metadata']['name']}")
            else:
                self.logger.error(f"Exception when creating/updating ImageCache: {e}")

    def get_image_domain(self, image: str) -> str:
        tmp = image.split("/")
        return tmp[0] if len(tmp) > 0 else ""
    
    def is_docker_hub_image(self, image_name: str):
        if '/' in image_name and '.' in image_name.split('/')[0]:
            return False
        return True

    def get_new_image(self, image: str):
        if self.is_docker_hub_image(image):
            if len(image.split('/')) == 1:
                return f"{self.proxy_url}/docker.io/library/{image}"
            return f"{self.proxy_url}/docker.io/{image}"
        else:
            return f"{self.proxy_url}/{image}"

    def is_omit_image(self, image: str):
        return self.get_image_domain(image) in self.no_proxy_domains
    
    def clean(self, signum, frame):
            self.logger.info("clean up")
            for node in self.nodes:
                name = f"imagecache-node-{node}"
                api_instance = client.CustomObjectsApi()
                try:
                    api_instance.delete_namespaced_custom_object(
                        group="kubefledged.io",
                        version="v1alpha2",
                        namespace="kube-fledged",
                        plural="imagecaches",
                        name=name
                    )
                    self.logger.info(f"ImageCache deleted: {name}")
                except ApiException as e:
                    if e.status == 404:
                        self.logger.info(f"ImageCache {name} not found")
                    else:
                        self.logger.error(f"Exception when deleting ImageCache: {e}")
            exit(0)

    def run(self):
        self.logger.info("Start watching pods")
        w = watch.Watch()
        v1 = client.CoreV1Api()
        while True:
            items: dict[str, list] = {}
            for event in w.stream(v1.list_pod_for_all_namespaces, timeout_seconds=3):
                if event['type'] == 'ADDED':
                    pod = event['object']
                    if pod.metadata.namespace == self.kube_fledged_namespace:
                        continue
                    image, reason = self.get_image_pull_state_info(pod)
                    nodename = self.get_pod_node(pod)
                    if image:
                        if self.is_omit_image(image):
                            continue

                        self.logger.info(f"handle pod {pod.metadata.name}, {pod.metadata.name=}, {pod.metadata.namespace=}, {reason=},{image=}, {nodename=}")

                        if self.allow_del_pod:
                            # 强制删除这个pod
                            v1.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace)

                        new_image = self.get_new_image(image)
                        if nodename not in items:
                            items[nodename] = set()
                        items[nodename].add(new_image)
            if not items:
                continue
            for node, images in items.items():
                self.nodes.add(node)
                self.logger.info(f"commit pull task, {node=}, {images=}")
                image_cache = self.create_image_cache(node, list(images))
                self.logger.debug(f"{image_cache=}")
                self.apply_image_cache(image_cache)
            time.sleep(self.image_check_interval)



def main():
    # 加载 kube 配置
    config.load_kube_config()

   # 从环境变量获取参数
    docker_proxy_url_env = os.getenv('DOCKER_PROXY_URL')
    if not docker_proxy_url_env:
        print("DOCKER_PROXY_URL is required")
        return
    no_proxy_docker_domain_env = os.getenv('NO_PROXY_DOCKER_DOMAIN', "")
    kube_fledged_namespace = os.getenv("KUBE_FLEDGED_NAMESPACE", "kube-fledged")
    image_check_interval = os.getenv("IMAGE_CHECK_INTERVAL", 10)

    image_puller = AutoDockerImagePull(proxy_url=docker_proxy_url_env, no_proxy_domain=no_proxy_docker_domain_env.split(","), kube_fledged_namespace=kube_fledged_namespace, image_check_interval=image_check_interval)
    image_puller.run()
    image_puller.clean()
    
if __name__ == '__main__':
    main()