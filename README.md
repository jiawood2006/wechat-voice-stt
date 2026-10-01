# 微信语音转文字 · WeChat Voice → Text

**把微信/企业微信语音（`.silk`）和任意音视频转成文字：本机只做解码，推理跑在你自己的服务器上，不上传第三方。**

- 🔒 **隐私**：音频不出自己的机器（客户端只把音频经 ssh 管道送给自己的服务器）
- ⚡ **快**：11.66s 语音 → **4.6~5.3s**（约 2.4x 实时）
- 🧠 **准**：内置**同音错词表**纠错（实测比换更大模型更有效）
- 🧾 **可审计**：每次调用写一条日志——走哪条通道、各段耗时、原文、纠错明细
- 🪫 **省**：可跑在 2 核 / 2G 的小服务器上，base 模型常驻内存约 390MB

```
微信语音 .silk
   │  pilk 解码
   ▼
24kHz wav ──► afconvert/ffmpeg 压成 AAC 64k（体积约 1/5）
   │  ssh 管道（长连接复用）
   ▼
服务器 whisper.cpp whisper-server 127.0.0.1:8004   ← 模型常驻，无需每次加载
   │  JSON
   ▼
文本 ──► 同音纠错表(asr_fix) ──► 写 txt + 写调用日志(JSON 行)
        （服务器不可用时自动回退本机 faster-whisper）
```

## 快速开始

**① 服务器（Ubuntu/Debian，2 核 2G 起步）**

```bash
git clone https://github.com/<you>/wechat-voice-stt && cd wechat-voice-stt
./install.sh server            # 编译 whisper.cpp + 下模型 + 装 systemd 服务
curl -s -F file=@sample.wav http://127.0.0.1:8004/inference   # 健康检查
```

服务只监听 `127.0.0.1:8004`，**不对外暴露**，客户端通过 ssh 调用。

**② 客户端（Mac / Linux）**

```bash
pip install pilk faster-whisper
export WHISPER_STT_HOST=myserver        # 你的 ssh 别名或 IP
python3 scripts/stt_client.py voice.silk            # 打印文字
python3 scripts/stt_client.py voice.silk -o ./out   # 写 out/voice.txt
python3 scripts/stt_client.py a.m4a --json          # 带耗时的 JSON
```

先配好免密登录：`ssh-copy-id myserver`。

**③ 纠错表（越用越准）**

```bash
scripts/asr_dict list
scripts/asr_dict add 打票 达标        # 把自己遇到的错词加进去
scripts/asr_dict test "这个反用速度要达标,俊工资料组决"
# → 这个响应速度要达标,竣工资料组卷
```

## 实测性能（真实微信语音，2 核 2G，base 模型）

| 语音时长 | 端到端耗时 | 速度比 | 说明 |
|---|---|---|---|
| 11.66s | **4.6~5.3s** | **2.4x 实时** | 解码 0.1s + 上传&推理 4.6s |
| 4.06s | 5.4s | 0.74x | 短语音的固定开销（ssh+ffmpeg 解码）占大头 |
| 30s+ | 约 8~15s | 2~3x | 时间越长越划算 |

优化手段与效果：**上传前压缩**（wav 190KB→35KB / 546KB→92KB）、**ssh 长连接复用**（免每次握手）合计省 0.3~0.4s。

## 准确率：别急着换大模型（实测结论）

| 做法 | 结果 | 结论 |
|---|---|---|
| base → `small-q5_1` | 13~17s（比说话还慢）、内存 548MB、**同一段样本并没有更准** | ❌ 不值 |
| 音量归一化 `loudnorm` | 结果逐字相同 | ❌ 无效 |
| 增大 beam size | 仅个别词变化 | ⚠️ 收益极小 |
| `--prompt` 加领域词 | 「组决 → 组卷」等被纠正 | ✅ 有效，成本为零 |
| **同音错词表纠错** | 响应→反用、达标→打票、竣工→俊工… 全部修正 | ✅ **最有效** |

**根因**：微信语音本身是低码率压缩（SILK），同音字歧义是通道固有上限，不是模型不够大。
所以本项目的思路是：**小模型跑得快 + 一张可审计的错词表兜底**。

## 接入 Hermes Agent（可选）

作为 STT 的 `local_command` 使用（参数兼容 openai-whisper CLI）：

```yaml
# ~/.hermes/config.yaml
stt:
  enabled: true
  provider: local_command
  local:
    command: "python3 /path/to/wechat-voice-stt/scripts/stt_client.py"
```

微信语音落到本机后由 Hermes 自动转写；也可手动重转任意一条：

```bash
python3 scripts/stt_client.py ~/.hermes/cache/audio/xxx.silk
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WHISPER_STT_HOST` | `stt-server` | 服务器 ssh 别名/IP |
| `WHISPER_STT_URL` | `http://127.0.0.1:8004/inference` | 服务器上的推理地址 |
| `WHISPER_STT_REMOTE` | `1` | 设 `0` 只用本机 faster-whisper |
| `WHISPER_STT_M4A` | `1` | 设 `0` 关闭上传前压缩 |
| `WHISPER_STT_LOGFILE` | `~/.hermes/logs/voice_stt_calls.log` | 调用日志（0600） |
| `ASR_FIX` | `1` | 设 `0` 关闭同音纠错 |
| `ASR_FIX_DICT` | 脚本同目录 `asr_fix_dict.json` | 自定义错词表 |

## 踩坑清单（都踩过）

1. **微信语音是 `.silk`**，普通播放器/识别工具解不开 → 用 `pilk` 解码成 wav。
2. **模型必须从 `ggerganov/whisper.cpp` 下**（`ggml-org/...` 会 404）；国内直连 huggingface.co 常超时，用 `hf-mirror.com`（限速约 100KB/s）。
3. **下载可能悄悄损坏**：先比对官方 `Content-Length`，不符就重下（否则报 `failed to initialize whisper context`）。
4. `whisper-server` 的 `--convert` 依赖 **ffmpeg**；不装则只能收 wav。
5. 2 核机器**同时只跑一个转写**，并发会互相抢 CPU 且 small 模型 548MB 有 OOM 风险。
6. 日志是排查「到底走了哪条通道」的**唯一可靠证据**（别靠猜）。
7. `.silk` 是微信自有的压缩格式——本项目只做本地解码，不依赖微信客户端。

## English

`wechat-voice-stt` turns WeChat/WeCom voice messages (and any audio/video) into text using a
self-hosted `whisper.cpp` server: the client only decodes the `.silk` file and pipes a compressed
copy over ssh to your own box — **nothing goes to a third-party API**. Ships with a deterministic
homophone-correction dictionary (measured to beat simply using a bigger model) and an auditable
per-call JSON log. 2x realtime on a 2-core / 2GB VPS with the `base` model.

## License

MIT — 见 [LICENSE](LICENSE)。
