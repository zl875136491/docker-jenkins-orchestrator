# 应用访问入口 API

## 用途

主程序在构建成功后，或在应用详情页加载时，调用本接口取得**可由用户访问的应用入口**。接口只返回 Docker Swarm 中已发布的 TCP 端口对应的 URL；不会把 overlay 网络内的服务名当成浏览器可访问地址。

```http
GET /api/v1/jenkins/app_access/{app_id}
Authorization: Bearer <access_token>
```

- `app_id`：应用 ID，等同于响应体的 `appid`。
- 认证：使用 OAuth2 access token，参见 [API Reference](api-reference.md#oauth2)。
- 成功：`200 OK`；应用不存在：`404 Not Found`；令牌缺失或无效：`401 Unauthorized`。

示例：

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://10.32.12.110:18080/api/v1/jenkins/app_access/linkwarden-94442740cc7a4d4e84d31a396f273ac7
```

## 响应契约

```ts
type PublishedPort = {
  target_port: number;       // 容器内目标端口，例如 3000
  published_port: number | null; // Swarm 对外发布端口，例如 18089
  protocol: "tcp" | "udp" | "sctp";
  mode: "ingress" | "host";
};

type ServiceAccess = {
  build_id: string;
  service_name: string;
  status: string;
  image: string;
  endpoint: string | null;
  published_ports: PublishedPort[];
  access_urls: string[];
  access_available: boolean;
  access_reason: string | null;
};

type AppAccess = {
  appid: string;
  services: ServiceAccess[];
  access_available: boolean;
  access_urls: string[];
  access_reason: string | null;
};
```

字段说明：

| 字段 | 主程序的处理方式 |
|---|---|
| `appid` | 校验它与请求的 `app_id` 一致，并作为应用详情数据的归属 ID。 |
| `access_available` | 应用级访问能力的唯一判定字段。为 `true` 时展示访问入口；为 `false` 时不展示“打开应用”操作。 |
| `access_urls` | 应用级、已去重的可访问 URL 列表。直接作为链接的 `href` 使用，不要自行拼接主机名或端口。 |
| `access_reason` | 应用无入口时的可展示原因；有入口时为 `null`。 |
| `services` | 部署服务明细，适合“运行资源”或诊断视图，不应用于判定整个应用是否可访问。 |
| `service_name` | Swarm 服务名，仅用于诊断、日志和服务管理，不能作为浏览器 URL 主机名。 |
| `endpoint` | 兼容性/部署元数据，形如 `service-name:18089`。它不是公网或浏览器访问地址，主程序不得使用它跳转。 |
| `published_ports` | Swarm 端口发布详情。用于诊断或展示端口映射；主程序不应据此自行构造 URL。 |
| 服务级 `access_available` | 仅表示该**服务**是否存在用户可访问的 URL。依赖服务为 `false` 是正常状态。 |
| 服务级 `access_reason` | 该服务没有访问 URL 的原因，例如 `No published ports`。仅在运行资源/诊断视图中展示。 |

`access_urls` 中的每一项都是普通 JSON 字符串，例如 `"http://10.32.12.110:18089"`。如果文档、聊天或页面将其显示为 Markdown 链接（`[...](...)`），那是渲染层的格式，不是 API 返回值。

## Linkwarden 示例解读

对于 Linkwarden，三个服务组成一个应用：

| 服务 | 是否应向用户展示入口 | 原因 |
|---|---|---|
| `...-linkwarden` | 是 | 其 `published_ports` 中有 TCP `18089 -> 3000`，并返回 `http://10.32.12.110:18089`。 |
| `...-linkwarden-meilisearch` | 否 | 它是应用内部搜索依赖，没有 published port。 |
| `...-linkwarden-postgres` | 否 | 它是应用内部数据库，没有 published port。 |

因此，虽然两个服务的 `access_available` 为 `false`，整个应用的 `access_available` 仍为 `true`，主程序应把应用入口显示为：

```text
http://10.32.12.110:18089
```

不要将 `No published ports` 视为 Linkwarden 应用失败。它只描述对应的内部依赖服务没有面向用户暴露端口。

## 推荐主程序逻辑

1. 构建处于非终态时，继续查询构建状态；不要使用本接口判断构建是否完成。
2. 构建成功后请求 `app_access/{app_id}`。
3. `access_available === true`：展示 `access_urls` 中每个 URL 的“打开应用”链接。URL 可能不止一个。
4. `access_available === false`：隐藏访问按钮，展示 `access_reason`；可提供“查看运行资源”以显示各服务原因。
5. 只在诊断视图展示 `services`、`endpoint` 与 `published_ports`。不要让内部依赖服务的状态覆盖应用级访问状态。

最小 TypeScript 示例：

```ts
async function loadAppAccess(apiBase: string, token: string, appId: string): Promise<AppAccess> {
  const response = await fetch(
    `${apiBase}/api/v1/jenkins/app_access/${encodeURIComponent(appId)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) throw new Error(`Unable to load app access: ${response.status}`);
  return response.json() as Promise<AppAccess>;
}

function accessView(data: AppAccess) {
  if (!data.access_available) {
    return { available: false, reason: data.access_reason ?? "No external access available", urls: [] };
  }
  return { available: true, reason: null, urls: data.access_urls };
}
```

## 无入口时的处理

常见 `access_reason` 及处理方式：

| 原因 | 含义 | 运维/交付处理 |
|---|---|---|
| `当前应用没有已部署服务` | 当前应用还没有可查询的部署记录。 | 检查构建和部署状态。 |
| `No published ports` | 服务没有对外发布端口。 | 在最终 Compose 产物中增加如 `"18082:3000"` 的 `ports` 配置，然后重新构建部署。 |
| `No TCP published ports` | 已发布的端口不能生成 HTTP(S) URL，例如只发布 UDP。 | 发布 TCP 端口，或通过适合该协议的访问方式交付。 |
| `Public host is not configured` | 平台未配置 `ORCHESTRATOR_PUBLIC_HOST`。 | 由平台运维配置公共主机和协议后重新查询。 |

`ports` 是外部入口配置；`expose` 仅用于容器网络内的服务发现，不能产生 `access_urls`。
