# Omni Visual RPA Fallback

`omni-bot-sdk-oss` is vendored under `third_party/omni-bot-sdk-oss/` as an
optional visual RPA fallback. It is disabled by default.

## Enable

Set the driver order in the JSON config:

```json
{
  "wechat_driver_order": ["uia", "omni_visual"]
}
```

The UIA/pyweixin driver remains the primary path. The visual driver is only
used when UIA is unavailable or an action is explicitly unsupported by a prior
driver. It currently supports only:

- `send_message`
- `open_chat`

Friend requests, accepting friends, moments, and message scanning remain UIA-only
because the OSS visual handlers are missing or intentionally not used as safe
fallbacks.

## Pure Visual Test CLI

Use these commands when you want to test only the visual RPA path. They do not
enqueue tasks, start the worker, or run UIA bootstrap.

```powershell
wxclient visual-open-chat "文件传输助手" --real
wxclient visual-send-message "文件传输助手" "视觉RPA测试" --real
```

Without `--real`, the commands dry-run and will not initialize Omni.

## Dependencies

Do not install the visual RPA dependencies into the main wxauto environment by
default. They require Python 3.12 and pin packages that conflict with this
project, including `openai==1.93.0`.

Use `requirements-omni-visual-rpa.txt` only for a dedicated Python 3.12 runtime.

## Source

Context7 did not return a valid `weixin-omni/omni-bot-sdk-oss` library ID. The
integration is based on the vendored GitHub source recorded in
`third_party/omni-bot-sdk-oss.SOURCE.md`.
