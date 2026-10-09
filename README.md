# 长江雨课堂刷视频脚本

#### 介绍

长江雨课堂（https://changjiang.yuketang.cn/）自动刷课脚本。

源自:

* https://github.com/heyblackC/yuketangHelper
* https://gitee.com/dtros/yuketangbrushvideo

> 本版本（v5）已按 `changjiang.yuketang.cn` \\\*\\\*MOOC 课程\\\*\\\*的真实接口重写并实测跑通，
> 修复记录与接口逆向说明见 \\\[FINDINGS.md](FINDINGS.md)。

\---

## 环境要求

* Python 3.8+
* `requests`（必需）、`colorama`（可选，仅用于终端颜色）

```bash
pip install requests colorama
```

## 获取 Cookie

1. 浏览器登录 https://changjiang.yuketang.cn/
2. 按 F12 → Application/存储 → Cookies → `https://changjiang.yuketang.cn`
3. 复制这三个值：

   * `csrftoken`
   * `sessionid`
   * `university\\\_id`（长江大学为 `2803`）

## 使用方法

```bash
# 方式一：命令行参数（推荐）
python main.py --csrftoken=你的值 --sessionid=你的值 --uv-id=2803 --course="全球化与世界空间"

# 方式二：环境变量
set YKT\\\_CSRFTOKEN=你的值
set YKT\\\_SESSIONID=你的值
set YKT\\\_UV\\\_ID=2803
set YKT\\\_COURSE=全球化与世界空间
python main.py

# 方式三：交互式（不传参数，按提示输入）
python main.py
```

`--course` 支持只写课程名的一部分；也可以不写 `--course`，程序会列出全部课程让你输入编号。

## 参数说明

|参数|说明|
|-|-|
|`--csrftoken`|Cookie 中的 csrftoken|
|`--sessionid`|Cookie 中的 sessionid|
|`--uv-id`|university\_id，长江大学填 `2803`|
|`--course`|课程名关键词，可只写一部分|
|`--index`|直接按课程列表编号选课|

## 已知限制

1. **`xtbz` 必须是 `ykt`**：脚本已内置。若服务端返回 `80019 请购买该课程之后再学习`，
说明账号尚未加入该 MOOC —— 请先在网页端加入课程。
2. 个别视频服务端元数据 `duration=0`（如本课程的 `leaf\\\_id=47039527`），
脚本会用同章视频时长估算，并以服务端返回的 `video\\\_length` 自动补齐。
3. 只处理视频（`leaf\\\_type=0`），不涉及作业、考试、讨论。
4. `sessionid` 会过期，报错“认证失败”时请重新获取。

### 须知

1. 如雨课堂网站地址：https://xxx.yuketang.cn/....（xxx 为自己学校的前缀）请使用如上大佬的脚本
2. 如雨课堂网站地址：https://changjiang.yuketang.cn/ 前缀固定为 changjiang 则可以使用本脚本

