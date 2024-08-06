import time
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

DOCKER_PRXOY_URL=xxx
OMIT_DOCKER_URL = []
def get_image_pull_state_info(pod):
    """
    检查 Pod 是否在镜像拉取阶段
    """
    if pod.status.container_statuses:
        for container_status in pod.status.container_statuses:
            if container_status.state.waiting:
                if container_status.state.waiting.reason in ["ImagePullBackOff", "ErrImagePull"]:
                    return container_status.image, container_status.state.waiting.reason
    return None, None

def get_pod_node(pod):
    """
    获取 Pod 调度到的节点
    """
    return pod.spec.node_name

def create_image_cache(node: str, data: list[str]):
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

def apply_image_cache(image_cache):
    """
    应用 ImageCache 资源
    """
    name = image_cache["metadata"]["name"]
    namespace = image_cache["metadata"]["namespace"]

    api_instance = client.CustomObjectsApi()
    # 如果存在就更新
 # 尝试获取现有的 ImageCache 资源
    try:
        existing_cache = api_instance.get_namespaced_custom_object(
            group="kubefledged.io",
            version="v1alpha2",
            namespace=namespace,
            plural="imagecaches",
            name=name
        )
        # 如果存在，更新 ImageCache 资源
        api_response = api_instance.patch_namespaced_custom_object(
            group="kubefledged.io",
            version="v1alpha2",
            namespace=namespace,
            plural="imagecaches",
            name=name,
            body=image_cache
        )
        print(f"ImageCache updated: {api_response['metadata']['name']}")
    except ApiException as e:
        if e.status == 404:
            api_response = api_instance.create_namespaced_custom_object(
                group="kubefledged.io",
                version="v1alpha2",
                namespace="kube-fledged",
                plural="imagecaches",
                body=image_cache
            )
            print(f"ImageCache created: {api_response['metadata']['name']}")
        else:
            print(f"Exception when creating/updating ImageCache: {e}")

def is_docker_hub_image(image_name):
    # 如果镜像名称中包含域名前缀，则认为它不是 Docker Hub 的镜像
    if '/' in image_name and '.' in image_name.split('/')[0]:
        return False
    return True

def get_new_image(image):
    if is_docker_hub_image(image):
        if len(image.split('/')) == 1:
            return f"{DOCKER_PRXOY_URL}/docker.io/library/{image}"
        return f"{DOCKER_PRXOY_URL}/docker.io/{image}"
    else:
        return f"{DOCKER_PRXOY_URL}/{image}"

def is_omit_image(image: str):
    if image.startswith("core-harbor.org"):
        return True
    return False

def main():
    # 加载 kube 配置
    config.load_kube_config()

    v1 = client.CoreV1Api()


    w = watch.Watch()
    while True:
        items: dict[str, list] = {}
        for event in w.stream(v1.list_pod_for_all_namespaces, timeout_seconds=3):
            if event['type'] == 'ADDED':
                pod = event['object']
                if pod.metadata.namespace == "kube-fledged":
                    continue
                image, reason = get_image_pull_state_info(pod)
                nodename = get_pod_node(pod)
                if image:
                    if is_omit_image(image):
                        continue
                    print(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} is in image {reason}, image: {image}, nodename: {nodename}")

                    # 强制删除这个pod
                    v1.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace)

                    new_image = get_new_image(image)
                    if nodename not in items:
                        items[nodename] = []
                    items[nodename].append(new_image)
        if not items:
            continue
        print(items)
        for node, images in items.items():
            image_cache = create_image_cache(node, list(images))
            print(f"node: {node}, image_cache: {image_cache}")
            apply_image_cache(image_cache)
        time.sleep(10)

if __name__ == '__main__':
    main()