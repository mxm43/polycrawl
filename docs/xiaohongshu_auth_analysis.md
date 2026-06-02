# 小红书鉴权机制分析与提取指南

> **目标**：理解小红书 Web API 的签名机制，掌握从网页中提取鉴权代码的方法，
> 并在鉴权代码变更时能够快速重新分析。

---

## 1. 概览 — 两张签名

小红书的 Web API 需要两张签名才能正常请求：

| Header | 名称 | 作用 | 特点 |
|--------|------|------|------|
| `x-s` | request signature | 每次请求唯一，证明请求未被篡改 | 纯数学计算，不依赖浏览器环境 |
| `x-s-common` | common signature | 证明请求来自"真实浏览器" | 携带浏览器指纹、a1 cookie、CRC32 校验 |

辅助 header：

| Header | 来源 |
|--------|------|
| `x-t` | 当前时间戳（毫秒） |
| `x-b3-traceid` | 16 位随机 hex |
| `x-xray-traceid` | 32 位随机 hex |

---

## 2. `x-s` 签名生成流程（核心）

### 2.1 浏览器侧流程（`seccore_signv2` 函数）

```javascript
function seccore_signv2(uri, params) {
    // Step 1: 构建 content string
    var content = uri;
    if (typeof params === 'object') {
        content += JSON.stringify(params);  // GET/POST 都 JSON
    } else if (typeof params === 'string') {
        content += params;
    }

    // Step 2: 计算两个 MD5
    var d_value = K.Pu(content);      // MD5(content_string)
    var m_value = K.Pu(uri);          // MD5(uri) —— GET 请求也是 MD5(uri)！

    // Step 3: 调用 window.mnsv2
    var result = window.mnsv2(content, d_value, m_value);

    // Step 4: 包装成 XYS_ 前缀
    var signature = {
        x0: "4.2.6",           // SDK 版本
        x1: "xhs-pc-web",      // app ID
        x2: "Windows",         // 平台
        x3: "mns0301_" + result,
        x4: ""                 // 类型标识
    };
    return "XYS_" + customBase64(JSON.stringify(signature));
}
```

### 2.2 `window.mnsv2(content, d_value, m_value)` 内部

这是核心签名函数，接受三个字符串参数，返回 Base64 编码的 144 字节签名。

**`window.mnsv2` 做了这些事：**

1. **构建 144 字节 payload 数组**（见下方布局）
2. **XOR 变换** — 用 144 字节密钥 `HEX_KEY` 逐字节 XOR
3. **自定义 Base64 编码** — 使用专用的 X3 字母表

**144 字节 payload 布局：**

| 偏移 | 长度 | 内容 | 说明 |
|------|------|------|------|
| 0 | 4 | `VERSION_BYTES` | 固定值 `[121, 104, 96, 41]` |
| 4 | 4 | seed | 随机 32 位整数（小端） |
| 8 | 8 | 时间戳 | 当前毫秒时间戳（小端） |
| 16 | 8 | page_load_time | 页面加载时间戳（有 session） |
| 24 | 4 | sequence | 请求序号（有 session 时递增） |
| 28 | 4 | window_props | 窗口属性长度（有 session 时递增） |
| 32 | 4 | uri_length | content string 的 UTF-8 长度 |
| 36 | 8 | MD5 XOR | `d_value` 前 8 字节 XOR `seed_byte` |
| 44 | 1 | a1_len | a1 cookie 长度字节 |
| 45 | 52 | a1_value | a1 cookie（补齐/截断至 52 字节） |
| 97 | 1 | app_len | app_id 长度字节 |
| 98 | 10 | app_id | `xhs-pc-web`（补齐/截断至 10 字节） |
| 108 | 15 | part11 | 环境检测表 |
| 123 | 4 | A3_PREFIX | 固定值 `[2, 97, 51, 16]` |
| 127 | 16 | A3_HASH | `custom_hash(ts_bytes + md5_path_bytes)` XOR `seed_byte` |
| **总长** | **143** | | 实际构建到 143（0-indexed），补齐至 144 |

> 注：浏览器 payload 的 `part11`（偏移 108~122）包含环境检测字节。
> xhshow/本项目使用"正常浏览器"的默认值，不做真实的运行时检测。

### 2.3 `custom_hash` 函数

用于计算 payload 末尾的 A3 字段。这是一个自定义非标准哈希：

```
输入: ts_bytes(8) + md5_path_bytes(16) = 24 字节
输出: 16 字节

1. 用 HASH_IV (4 个 32 位常数) 初始化 s0~s3
2. s0~s3 XOR 输入长度
3. 每 8 字节一组，解包为 2 个 32 位 int
4. 多轮 32 位左旋、加法和 XOR
5. 最终 4 个 s 值重新混合 → 16 字节输出
```

### 2.4 XOR 变换

```
output[i] = payload[i] ^ HEX_KEY[i]   # i < 144
```

`HEX_KEY` 是 144 字节的十六进制常量，从浏览器 JS 中提取。

---

## 3. `x-s-common` 签名生成流程

`x-s-common` 是一个携带浏览器指纹的 JSON 对象，经过自定义 Base64 编码。

### 3.1 指纹生成（`FingerprintGenerator.generate`）

生成 50+ 字段的浏览器指纹字典，包括：

| 字段 | 内容 | 来源 |
|------|------|------|
| `x1` | User-Agent | 固定值 |
| `x3` | 语言 | `zh-CN` |
| `x4` | 色深 | 随机 24/30/32 |
| `x7` | GPU 信息 | 随机从预设 GPU 列表选 |
| `x9` | 屏幕分辨率 | 加权随机 |
| `x12` | 时区 | `Asia/Shanghai` |
| `x43` | Canvas 指纹 | 固定值 `742cc32c` |
| `x57` | Cookie 字符串 | 从参数传入 |
| 等等 | | |

**这些字段都是模拟值**，不需要真实的浏览器环境。

### 3.2 `b1` 值生成

```
1. 从指纹中提取 18 个字段 (x33~x52, x82)
2. JSON 序列化 → RC4 加密 (密钥: "xhswebmplfbt")
3. URL 编码 → 解析 %XX 为字节 → 自定义 Base64 编码
```

### 3.3 `x-s-common` 最终结构

```json
{
    "s0": 5,
    "x0": "1",
    "x1": "4.3.3",
    "x2": "Windows",
    "x3": "xhs-pc-web",
    "x4": "4.86.0",
    "x5": "<a1_cookie>",
    "x8": "<b1_value>",
    "x9": <CRC32(b1)>,
    "x11": "normal"
}
```

→ `Json → 自定义 Base64 编码` → `x-s-common` header 值

---

## 4. 关键发现：为什么不需要"补环境"

### 4.1 `window.mnsv2` 是纯计算函数

从调用签名可以清楚看出：

```javascript
window.mnsv2(content_string, d_value, m_value)
//          ^^^^^^^^^^^^^  ^^^^^^  ^^^^^^
//          字符串          MD5串    MD5串
```

**它只接受三个字符串参数，返回一个字符串**。不访问 DOM、不读写 Cookie、
不调用 Canvas/WebGL、不发送网络请求。**全部是纯数学运算**：
- 字符串拼接
- MD5 哈希
- 144 字节数组构建
- XOR 变换
- Base64 编码

### 4.2 xhshow 和本项目的做法

xhshow 和我们的本地实现直接在 Python 中重写了 `window.mnsv2` 的所有运算：
- 用 `hashlib.md5()` 替代 `K.Pu()`
- 用 Python 列表操作替代 JavaScript 的 TypedArray 操作
- 用自定义 Base64 编码器替代 `btoa()` + 字母表替换

**不需要 eval 混淆的 JS 代码，所以不需要补环境。**

### 4.3 那什么时候才需要"补环境"？

只有当**直接运行混淆后的 JS** 时才需要：

| 方案 | 需要补环境？ | 原因 |
|------|-------------|------|
| Node.js eval vendor JS | ✅ 需要 | obfuscated code 会检查 `window`、`document`、`navigator` 等 |
| Playwright 浏览器 | ✅ 自动有 | 真实浏览器环境 |
| Python 重写算法 | ❌ 不需要 | 直接实现算法，不运行 JS |

### 4.4 环境检查都做了什么

混淆 JS 中的环境检查（就是"补环境"要对付的那些）主要是：

1. **防御性空值检查**：访问 `window.foo`，如果不存在就 `try/catch`，不影响签名结果
2. **属性存在性检查**：`'onload' in window` 之类的，签名结果不含这些信息
3. **类型欺骗**：部分检查会生成一些中间值，但最终 payload 里用的是固定默认值

**核心结论**：payload 中的 `part11`（偏移 108~122）虽然名称为"环境检测"，
但在 xhshow 和本项目中，我们都使用 `ENV_CHECKS_DEFAULT` 的固定值，
因为浏览器侧的正常环境检查结果就是这些值。

---

## 5. xhshow/本地实现 vs 浏览器行为差异

### 5.1 已修正的差异（xhshow PR#106 修复）

| 项目 | 浏览器 | xhshow 旧版 | PR#106/本项目 |
|------|--------|------------|--------------|
| DATA_SDK_VERSION | `4.3.3` | `4.2.6` | `4.3.3` ✅ |
| DATA_webBuild | `6.13.3`（实际） | `5.0.3` | `6.3.0`（可覆盖）|
| GET content string | `uri + JSON.stringify(params)` | `uri?key=value` | ❌ `uri?key=value` |
| POST content string | `uri + JSON.stringify(params)` | `uri + JSON.stringify(params)` | ✅ |
| m_value (GET) | `MD5(uri)` | `d_value` | `d_value` |
| m_value (POST) | `MD5(uri)` | `MD5(uri)` | `MD5(uri)` ✅ |
| 自定义哈希参数 | `md5_path_bytes`（full MD5 bytes） | `extract_api_path` | `md5_path_bytes` ✅ |

### 5.2 需要关注的差异

**Content string 的构建方式**（GET 请求）：

xhshow 的 `_build_content_string` 对 GET 使用 `uri?key=url-encoded-value` 格式，
但浏览器实际使用 `uri + JSON.stringify(params)`。

这是浏览器 `seccore_signv2` 函数的行为——它对 GET 和 POST 都用 JSON.stringify。
不过 xhshow 用 `uri?key=value` 格式也能通过服务端验证，说明
**小红书服务端可能兼容了两种 content string 格式**，或者在签名验证前做了标准化。

如果未来某天出现 406 签名错误，应该优先检查 content string 的格式。

### 5.3 Session 状态的作用

`SessionManager` 模拟了浏览器页面加载后的状态变化：

| 参数 | 含义 | 变化规律 |
|------|------|---------|
| `page_load_timestamp` | 页面加载时间 | 固定，创建时设置 |
| `sequence_value` | 操作序列号 | 每次请求递增 0~1 |
| `window_props_length` | window properties | 每次请求递增 1~10 |

这些值使每次签名看起来来自同一个浏览器页面会话。

---

## 6. 从网页提取鉴权代码的步骤

当 API 返回 401/406 错误时，说明签名算法已更新。按以下步骤提取新的鉴权参数。

### 步骤 1：找到 vendor JS 文件

打开浏览器 DevTools → Network 标签 → 过滤 `vendor-dynamic`：

```
https://sns-webpic-qc.xhscdn.com/.../vendor-dynamic.XXXXXXXX.js
```

文件名中的 hash 每次更新会变化。下载该文件。

### 步骤 2：定位 `signV2Init` 函数

在 vendor JS 中搜索 `"signV2Init"` 字符串，找到包含它的代码块。

`signV2Init` 的函数体（不是外层包裹代码）包含一个特殊的赋值语句：

```javascript
String.raw(__makeTemplateObject([...], [...]))
```

这就是被混淆的真实 mnsv2 源码，被嵌入为 JavaScript 的 tagged template literal。

### 步骤 3：理解模板字符串混淆机制

#### `__makeTemplateObject` 是什么

这是 TypeScript/Webpack 编译 ES6 模板字符串时产生的辅助函数：

```javascript
function __makeTemplateObject(cooked, raw) {
    Object.defineProperty(cooked, "raw", { value: raw });
    return cooked;
}
```

在 vendor JS 中，`String.raw(...)` 的参数是 `__makeTemplateObject(cooked_array, raw_array)`。

#### 两个数组的含义

```javascript
__makeTemplateObject(
    ["line1", "line2", ..., "lineN"],   // cooked — 转义后的字符串片段
    ["line1", "line2", ..., "lineN"]    // raw — 原始（未转义）字符串片段
)
```

- **`cooked` 数组**：JS 引擎解析后的字符串，转义序列已被处理
- **`raw` 数组**：原始字符串，`\\n` 仍为 `\\n`、`\\` 仍为 `\\` 等

**对于去混淆，必须使用 `raw` 数组**，因为模板字符串中的控制字符在 `cooked` 中会被错误解析。

#### 模板字符串拼接规则

`String.raw()` 的语义是：

```javascript
// String.raw(\`...${expr1}...${expr2}...\`)
// 等价于: raw[0] + expr1 + raw[1] + expr2 + raw[2]
```

在 `signV2Init` 中，`__makeTemplateObject` 的两个数组之间还**穿插了表达式**：

```javascript
String.raw(__makeTemplateObject(
    [raw[0], raw[1], ..., raw[N]],
    [raw[0], raw[1], ..., raw[N]]
), expr1, expr2, ..., exprN)
```

正确的拼接方式是：

```
output = raw[0] + expr1 + raw[1] + expr2 + ... + raw[N]
```

### 步骤 4：自动去混淆（推荐）

项目 `tmp/` 目录下有两个工具脚本：

#### Node.js 版本（`tmp/gen_mnsv2.js`）

```javascript
// 从 signV2Init 代码中提取 __makeTemplateObject 的两个数组参数
// 用 String.raw 的行为拼接 raw[i] + expression[i] + raw[i+1]
// 输出到 tmp/mnsv2_source.js
```

用法：
```bash
node tmp/gen_mnsv2.js
```

#### Python 版本（`tmp/gen_mnsv2.py`）

```python
# 从下载的 vendor-dynamic.XXXX.js 中：
# 1. 正则提取 __makeTemplateObject([...],[...]) 的两个数组
# 2. 提取后面的表达式列表
# 3. 按 raw[0] + expr1 + raw[1] + ... 拼接
# 4. 输出去混淆后的临时 JS 文件
```

### 步骤 5：手动去混淆（当工具脚本失效时）

如果模板结构有变化，需要手动处理。提取 `raw` 数组的步骤：

#### 5a. 找到 `__makeTemplateObject` 调用的两个数组

在 vendor JS 中，`signV2Init` 附近的结构类似：

```javascript
var signV2Init = function() {
    // ... 外层包裹代码 ...
    return String.raw(__makeTemplateObject(
        // 第一个数组 (cooked)
        ["\x00\\x00\\x00...", "\x00\\x00\\x00...", ...],
        // 第二个数组 (raw)  ← 我们只需要这个
        ["\\x00\\\\x00\\\\x00...", "\\x00\\\\x00\\\\x00...", ...]
    ), expr1, expr2, ..., exprN);
};
```

#### 5b. 提取 `raw` 数组的元素

`raw` 数组中的每个元素是一条"模板片段"。注意：

- 元素中可能有 `\\xXX`、`\\uXXXX` 等转义——这是经过 JSON/字符串双重编码的
- 正确的方法是：将这些字符串传给 `String.raw()`，或者直接拼接它们
- 每个元素末尾可能缺少字符（被插值表达式替代），所以拼接时一定要穿插表达式

#### 5c. 处理模板字符串的转义层级

这是最关键的环节。混淆代码中用了大量转义来隐藏真实字符：

| 在 vendor JS 中看到 | 实际值（Python 解码后） |
|-------------------|----------------------|
| `\\x` | `\x`（单反斜杠 + x） |
| `\\\\` | `\\`（双反斜杠） |
| `\\"` | `\"`（转义引号） |
| `\\n` | `\n`（换行符） |

**核心技巧**：把 `raw` 数组元素放进 Python 的 `codecs.decode(s, 'unicode_escape')` 可以展开一层。或者用 `bytes(s, 'utf-8').decode('unicode_escape')`。

但最可靠的方式是直接使用 `String.raw()` 的 JS 语义：
将 `raw[i]` 和 `expr_i` 交替拼接，然后把结果写入文件，这就是可读的 JS 源码。

### 步骤 6：从去混淆源码中提取常量

在生成的去混淆 JS 文件中搜索以下关键常量并更新到 `xs_config.py`：

1. **SDK 版本号** — 搜索 `"4."` 或 `DATA_SDK_VERSION`
   - `DATA_SDK_VERSION` — 通常在变量赋值中（当前 `"4.3.3"`）
   - `DATA_webBuild` — 另一个版本字符串（当前 `"6.3.0"`，浏览器实际发送 `"6.13.3"`）

2. **Base64 字母表** — 搜索类似 `"ABCDEFGHIJKLMNOPQRSTUVWXYZ"` 的 64 字符字符串
   - `CUSTOM_BASE64_ALPHABET` — 查找与标准字母表不同的 64 字符字符串
   - `X3_BASE64_ALPHABET` — x3 签名使用的字母表

3. **HEX_KEY** — 搜索 288 字符（144 字节）的十六进制字符串 `[0-9a-f]{288}`
   - 通常在 XOR 变换函数附近

4. **HASH_IV** — 搜索 `_custom_hash_v2`（或对应函数名）的实现，查找开头的 4 个 32 位常数

5. **VERSION_BYTES** — 搜索 payload 构建函数开头的 4 个硬编码字节

6. **ENV_TABLE / ENV_CHECKS_DEFAULT** — 搜索 part11 构建代码（15 字节的数组）

7. **X3_PREFIX** — 搜索 `"mns0301_"`（如果没变）

8. **SIGNATURE_DATA_TEMPLATE** — 搜索 `"XYS_"` 附近，找到 JSON 模板

### 步骤 7：验证

用新提取的常量运行签名测试：

```python
from packages.providers.xiaohongshu.xs_config import CryptoConfig
from packages.providers.xiaohongshu.xs_signer import XHSignatureSigner

cfg = CryptoConfig().with_overrides(
    DATA_SDK_VERSION="新版本",
    DATA_webBuild="新版本",
    CUSTOM_BASE64_ALPHABET="新字母表",
)
signer = XHSignatureSigner(cfg)
headers = signer.sign_headers_get("/api/sns/web/v1/user_posted", cookies, params={...})
# 用 httpx 发送请求检查状态码
```

---

## 7. 哪些值"需要"从浏览器获取，但实际可以绕过

> **核心原则**：签名体系中的每一个值，要么是硬编码常量（从 JS 提取一次就够），
> 要么是随机生成的值（不依赖浏览器环境）。唯一真正需要浏览器的是 cookies。

### 7.1 `x-s` 签名 — 全部可绕过

`x-s` 签名的生成**完全不依赖浏览器运行时环境**，所有输入都是计算得出：

| 输入 | 来源 | 能否绕过 | 说明 |
|------|------|---------|------|
| `content_string` | 本地构造 | ✅ 完全绕过 | 根据 URI 和 params 直接用代码拼接，与浏览器结果一致 |
| `d_value` (MD5) | 本地计算 | ✅ 完全绕过 | `hashlib.md5(content_string)` |
| `m_value` (MD5) | 本地计算 | ✅ 完全绕过 | GET: `d_value`；POST: `hashlib.md5(uri)` |
| `a1_value` | 浏览器 Cookie | ❌ **必须从浏览器获取** | 但这是身份令牌，不是环境检测 |
| `timestamp` | 本地生成 | ✅ 完全绕过 | `time.time()`，与 `x-t` header 保持一致即可 |
| `seed` | 本地随机 | ✅ 完全绕过 | `random.randint()`，服务端不校验具体值 |
| `page_load_timestamp` | 本地生成 | ✅ 完全绕过 | 首次请求时设为 `time.time()`，后续保持不变 |
| `sequence_value` | 本地生成 | ✅ 完全绕过 | 随机初始值，每次递增，模拟用户操作序列 |
| `window_props_length` | 本地生成 | ✅ 完全绕过 | 随机初始值，每次递增，模拟窗口属性变化 |
| `uri_length` | 本地计算 | ✅ 完全绕过 | `len(content_string.encode('utf-8'))` |
| `part11` 环境检测 | 固定默认值 | ✅ 完全绕过 | 浏览器环境检测结果永远是正常值，可直接硬编码 |

**小结**：`x-s` 的 144 字节 payload 中，除了 `a1_value`（取自身份 cookie），
**没有任何一个字节需要真实的浏览器环境**。

### 7.2 `x-s-common` 指纹 — 全部可伪造

`x-s-common` 中的所有 50+ 个指纹字段都是"模拟数据"，浏览器**不会验证**这些值的真实性：

| 字段 | 真实浏览器会怎么取 | 我们的做法 | 能否绕过 |
|------|-------------------|-----------|---------|
| `x1` (UA) | `navigator.userAgent` | 硬编码字符串 | ✅ 完全绕过 |
| `x3` (语言) | `navigator.language` | `"zh-CN"` | ✅ |
| `x4` (色深) | `screen.colorDepth` | 加权随机 24/30/32 | ✅ |
| `x5` (内存) | `navigator.deviceMemory` | 加权随机 2/4/8GB | ✅ |
| `x7` (GPU) | `canvas.getContext('webgl').getParameter(...)` | 从预设 80+ 条 GPU 字符串随机选 | ✅ |
| `x8` (CPU) | `navigator.hardwareConcurrency` | 加权随机 4/6/8 | ✅ |
| `x9` (分辨率) | `screen.width + ";" + screen.height` | 加权随机 1920x1080 / 1366x768 | ✅ |
| `x12` (时区) | `Intl.DateTimeFormat().resolvedOptions().timeZone` | `"Asia/Shanghai"` | ✅ |
| `x19` (平台) | `navigator.platform` | `"Win32"` | ✅ |
| `x21` (插件) | `navigator.plugins` | 固定字符串 | ✅ |
| `x22` (WebGL) | 实际 WebGL 调用 | **随机 MD5** | ✅ **浏览器不校验一致性** |
| `x43` (Canvas) | canvas.toDataURL() 后 MD5 | **固定值** `"742cc32c"` | ✅ Canvas 指纹在不同浏览器间差异极小 |
| `x53` (随机) | 生成随机 MD5 | 随机 MD5 | ✅ |
| `x54` (语音) | 语音合成 API | 固定值 `"10311144241322244122"` | ✅ |
| `x55` (字体度量) | 实际测量字体 | 固定字符串 | ✅ |
| `x56` (GPU 详情) | 实际 WebGL 参数 | 从预设 GPU + 随机 MD5 拼凑 | ✅ |
| `x57` (Cookie) | `document.cookie` | 从参数传入 | ✅ **传什么就是什么** |
| `x66` (页面 URL) | `window.location` | `"https://www.xiaohongshu.com/explore"` | ✅ |
| `x78` (字体渲染) | 实际测量页面上渲染的字体 | 固定预设值 | ✅ |
| `x82` | `Object.keys(window)` 检测 | `"_0x17a2|_0x1954"` | ✅ |

**关键洞察**：服务端对指纹的"校验"仅限于——格式是否正确、RC4 解密后是否是合法 JSON、
CRC32 是否匹配。至于指纹内容是否与真实浏览器一致，服务端**无法验证**，
因为服务器根本不知道你用的是哪款 GPU、何种屏幕分辨率。

### 7.3 `b1` 值生成 — 纯计算，无环境依赖

```
fingerprint(50+字段, 全是模拟的)
    → 提取 18 个字段 → JSON 序列化
    → RC4 加密 (密钥 "xhswebmplfbt" 是常量)
    → URL 编码 → 解析 %XX → 自定义 Base64
    → CRC32 得到 x9
```

整个流程没有一步需要浏览器环境。

### 7.4 辅助 Header — 全部可绕过

| Header | 真实浏览器 | 我们的做法 |
|--------|-----------|-----------|
| `x-t` | `Date.now()` | `int(time.time() * 1000)` |
| `x-b3-traceid` | `Math.random().toString(16)` | `random.choices(HEX_CHARS, k=16)` |
| `x-xray-traceid` | 时间戳 + 序列号 + 随机 | 时间戳 + 随机 |

### 7.5 小结：唯一不能绕过的

整个小红书签名体系中，**唯一必须从真实浏览器获取的只有 cookies**：

| Cookie | 用途 | 有效期 |
|--------|------|--------|
| `a1` | 用户身份令牌 + 签名密钥的一部分 | 数天~数周 |
| `web_session` | 会话令牌 | 数小时~数天 |
| `acw_tc` | 安全验证 | 不定，每次刷新页面可能变 |

这些 cookies 携带了服务端颁发的身份凭证，无法伪造。
但从浏览器复制一次后，可以在服务器端复用很长时间。

> **结论**：你不需要在服务器上装浏览器、不需要补任何环境、
> 不需要 Playwright、不需要 headless Chrome。
> 只需要：**Python + 正确的常数值 + 有效的 cookies = 完美的签名**。



---

## 8. 常见问题排查

### 8.1 HTTP 406 — 签名被拒绝

可能原因（按可能性排序）：

1. **SDK 版本过时** — 更新 `DATA_SDK_VERSION` 和 `DATA_webBuild`
2. **Content string 格式不匹配** — 检查 GET 请求的 content string 格式
3. **a1 cookie 过期** — 从浏览器复制新的 cookies
4. **Base64 字母表变更** — 检查 `CUSTOM_BASE64_ALPHABET` 和 `X3_BASE64_ALPHABET`
5. **HEX_KEY 变更** — 144 字节 XOR 密钥更新
6. **Payload 布局变更** — 144 字节结构中某个字段偏移变化

### 8.2 HTTP 401 — 未授权

- a1 cookie 失效，需要从浏览器重新获取

### 8.3 HTTP 200 + success: false

- Cookies 中的其他字段（`web_session`、`acw_tc` 等）过期
- 请求参数错误（如 user_id 不存在）

---

## 9. 文件结构参考

```
packages/providers/xiaohongshu/
├── __init__.py          # XiaohongshuProvider (content fetch)
├── xhs_signer.py        # 供给 provider 调用的高层封装 (fetch_notes)
├── xs_signer.py         # XHSignatureSigner 主签名器
├── xs_crypto.py         # CryptoProcessor (144 字节 payload 构建)
├── xs_config.py         # CryptoConfig (所有魔数常量)
├── xs_encoder.py        # Base64 / CRC32 / XOR / URL / MD5 helpers
├── xs_fingerprint.py    # 浏览器指纹 + x-s-common 签名
```

### 更新策略

当鉴权更新时，通常只需要修改 `xs_config.py` 中的常量值。
如果 payload 布局或算法变更，则需修改 `xs_crypto.py`。
