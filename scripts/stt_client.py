#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stt_client.py — 微信/企微语音（.silk）与任意音视频 → 文字，走自建 whisper.cpp 服务
=================================================================================
设计目标：**快**（base 模型，中文语音约 2x 实时）+ **省**（本机只做解码，推理在服务器）
        + **可审计**（每次调用留一条日志：走的哪条路、各段耗时、原文、纠错明细）。

链路：.silk → (pilk) 24k wav → (afconvert) AAC 64k m4a → ssh 管道 → whisper-server /inference
      → 文本 → 同音纠错表 → 写文件 + 写调用日志
失败自动回退本机 faster-whisper（服务器连不上也能用）。

用法:
  python3 stt_client.py voice.silk                      # 打印文字
  python3 stt_client.py voice.silk -o ./out             # 写 ./out/voice.txt
  python3 stt_client.py a.m4a --language zh -o ./out
  python3 stt_client.py voice.silk --json               # 输出 JSON（含耗时/纠错明细）

也可以当作 Hermes Agent 的 stt 本地命令（local_command）使用，参数兼容 openai-whisper CLI：
  input_path [--model X] [--output_dir DIR] [--language zh] [--output_format txt]

环境变量:
  WHISPER_STT_REMOTE=0     只用本机 faster-whisper（不连服务器）
  WHISPER_STT_HOST=my-srv  ssh 别名/IP（默认 stt-server）
  WHISPER_STT_URL=http://127.0.0.1:8004/inference
  WHISPER_STT_M4A=0        关闭上传前压缩
  WHISPER_STT_LOGFILE=...  调用日志路径（默认 ~/.hermes/logs/voice_stt_calls.log）
  ASR_FIX=0                关闭同音纠错
依赖: 可选 pilk（.silk 解码）、afconvert（macOS 压缩，Linux 用 ffmpeg 代替也行）
"""
import argparse, json, os, pathlib, shutil, subprocess, sys, tempfile, time

HOST = os.environ.get("WHISPER_STT_HOST", "stt-server")
URL = os.environ.get("WHISPER_STT_URL", "http://127.0.0.1:8004/inference")
USE_REMOTE = os.environ.get("WHISPER_STT_REMOTE", "1") != "0"
USE_M4A = os.environ.get("WHISPER_STT_M4A", "1") != "0"
LOGFILE = os.environ.get(
    "WHISPER_STT_LOGFILE", os.path.expanduser("~/.hermes/logs/voice_stt_calls.log")
)
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=6",
    "-o", "ControlMaster=auto",       # 长连接复用：免掉每次 ssh 握手
    "-o", f"ControlPath=/tmp/ssh-stt-{os.getuid()}-%h-%p",
    "-o", "ControlPersist=300",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def record(entry: dict) -> None:
    """每次调用追加一条 JSON 行日志（0600），可审计、可统计。失败不影响转写。"""
    try:
        p = pathlib.Path(LOGFILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def decode_to_wav(src: str) -> str:
    """微信语音 .silk → 标准 24kHz/16bit/mono wav（临时文件）；其他格式原样返回。"""
    if pathlib.Path(src).suffix.lower() != ".silk":
        return src
    try:
        import pilk  # pip install pilk
    except ImportError:
        raise SystemExit("需要 pilk 解码微信语音：pip install pilk")
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    pilk.silk_to_wav(src, tmp.name)
    return tmp.name


def compress_for_upload(wav: str) -> str:
    """wav → AAC 64k 单声道 m4a（体积约 1/5，上传更快）。失败则原样返回。"""
    if not USE_M4A:
        return wav
    if shutil.which("afconvert"):                     # macOS
        cmd = ["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", wav, wav + ".up.m4a"]
    elif shutil.which("ffmpeg"):                      # Linux
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-c:a", "aac", "-b:a", "64k",
               "-ac", "1", wav + ".up.m4a"]
    else:
        return wav
    try:
        src_sz = os.path.getsize(wav)
        if src_sz < 60 * 1024:                        # 太小不值得压
            return wav
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        out = wav + ".up.m4a"
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) < src_sz:
            log(f"[stt] 压缩上传 {src_sz // 1024}KB → {os.path.getsize(out) // 1024}KB")
            return out
    except Exception as e:  # noqa: BLE001
        log(f"[stt] 压缩跳过: {type(e).__name__}: {e}")
    return wav


def audio_seconds(path: str) -> float:
    if shutil.which("afinfo"):
        try:
            out = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=10).stdout
            return round(float(out.split("estimated duration: ")[1].split(" sec")[0]), 2)
        except Exception:  # noqa: BLE001
            pass
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", path], capture_output=True, text=True,
                                 timeout=10).stdout
            return round(float(out.strip()), 2)
        except Exception:  # noqa: BLE001
            pass
    return -1.0


def server_transcribe(audio: str):
    """经 ssh 把音频 POST 给服务器 whisper-server；返回 (文本|None, 耗时秒)。"""
    if not USE_REMOTE:
        return None, 0.0
    cmd = ["ssh", *SSH_OPTS, HOST,
           f"curl -s -m 600 -F file=@- -F response_format=json {URL}"]
    t0 = time.time()
    try:
        with open(audio, "rb") as f:
            r = subprocess.run(cmd, stdin=f, capture_output=True, timeout=660)
        el = round(time.time() - t0, 2)
        if r.returncode != 0:
            log(f"[stt] 服务器不可用 rc={r.returncode}: {r.stderr.decode('utf-8', 'ignore')[:200]}")
            return None, el
        text = (json.loads(r.stdout.decode("utf-8", "ignore") or "{}").get("text") or "").strip()
        if text:
            log(f"[stt] 服务器转写成功（{len(text)} 字，{el}s）")
            return text, el
        log("[stt] 服务器返回空，回退本机")
        return None, el
    except Exception as e:  # noqa: BLE001
        return None, round(time.time() - t0, 2)


def local_transcribe(audio: str, model: str, language: str):
    """回退路径：本机 faster-whisper（pip install faster-whisper）。"""
    from faster_whisper import WhisperModel
    t0 = time.time()
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(audio, language=language or None)
    return "".join(s.text for s in segs).strip(), round(time.time() - t0, 2)


def transcribe(src: str, model: str = "small", language: str = "zh") -> dict:
    """完整链路，返回结果字典（含耗时与纠错明细）。"""
    if not os.path.exists(src):
        raise SystemExit(f"文件不存在: {src}")
    t_start = time.time()
    wav = decode_to_wav(src)
    dur = audio_seconds(wav)
    upload = compress_for_upload(wav)
    try:
        text, infer_s = server_transcribe(upload)
        backend = "server"
        if text is None:
            log("[stt] 使用本机 faster-whisper")
            text, infer_s = local_transcribe(wav, model, language)
            backend = "local"
    finally:
        for p in {wav, upload}:
            if p != src:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    raw_text, subs = text, []
    try:                                              # 同音纠错（确定性、零成本、可审计）
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from asr_fix import fix
        text, subs = fix(text)
    except Exception as e:  # noqa: BLE001
        log(f"[stt] 纠错跳过: {type(e).__name__}: {e}")

    total = round(time.time() - t_start, 2)
    res = {
        "ts": time.strftime("%F %T"),
        "input": os.path.basename(src),
        "backend": backend,
        "audio_s": dur,
        "infer_s": infer_s,
        "total_s": total,
        "chars": len(text),
        "rtf": (round(dur / total, 2) if dur and total else None),
        "subs": subs,
        "raw_text": raw_text,
        "text": text,
    }
    record(res)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="微信语音/音视频 → 文字（自建 whisper.cpp 服务）")
    ap.add_argument("input_path", help="音频/视频文件；微信语音 .silk 可直接传")
    ap.add_argument("--model", default="small", help="本机回退时用的模型（默认 small）")
    ap.add_argument("--output_dir", "-o", default=None, help="输出目录（写 <名字>.txt）")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--output_format", default="txt")
    ap.add_argument("--task", default="transcribe")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（含耗时/纠错明细）")
    a = ap.parse_args()

    res = transcribe(a.input_path, a.model, a.language)
    if a.output_dir:
        os.makedirs(a.output_dir, exist_ok=True)
        p = pathlib.Path(a.output_dir) / (pathlib.Path(a.input_path).stem + ".txt")
        p.write_text(res["text"] + "\n", encoding="utf-8")
        log(f"transcribed {a.input_path} -> {p} ({res['chars']} chars)")
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif not a.output_dir:
        print(res["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
