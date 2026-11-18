"""Settings for MoviePilot's native form renderer (V2 and V3)."""

from pathlib import Path


_STYLE = Path(__file__).with_name("settings_style.css").read_text(encoding="utf-8")
_CONNECTIONS = r"(typeof cd2_confs === 'string' ? cd2_confs.replace(/^(?:[ \t]*\r?\n)+/, '') : '')"
_FIRST = _CONNECTIONS + r".split(/\r?\n/, 1)[0].split('#')"


def _node(component, props=None, *content):
    return {"component": component, "props": props or {}, "content": list(content)}


def _text(component, text, **props):
    return {"component": component, "props": props, "text": text}


def _expr(code):
    return "{{ " + code + " }}"


def _field(label, control, show=None):
    props = {"class": "cd2-field"}
    if show is not None:
        props["show"] = _expr(show)
    return _node("label", props, _text("span", label), control)


def _input(label, model, *, show=None, type="text", **props):
    return _field(label, _node("input", {
        "type": type, "value": _expr(f"{model} ?? ''"),
        "onInput": f"(event) => {{ {model} = event.target.value; }}", **props,
    }), show)


def _select(label, model, options, *, handler=None):
    return _field(label, _node(
        "select", {"value": _expr(model), "onChange": handler or f"(event) => {{ {model} = event.target.value; }}"},
        *(_text("option", title, value=value) for value, title in options),
    ))


def _switch(model, label, *, icon_only=False):
    return _node(
        "label", {"class": "cd2-switch"},
        _node("input", {
            "type": "checkbox", "role": "switch", "aria-label": label, "checked": _expr(model),
            "onChange": f"(event) => {{ {model} = event.target.checked; }}",
        }),
        *(() if icon_only else (_text("span", label),)),
    )


def _group(*children, show=None):
    props = {"class": "cd2-fields"}
    if show is not None:
        props["show"] = _expr(show)
    return _node("div", props, *children)


def _connection_fields():
    # Replace the first line only, including when upgrading an older username/password entry.
    parse = (
        f"const saved = {_CONNECTIONS}; const end = saved.indexOf('\\n'); "
        "const tail = end < 0 ? '' : saved.slice(end); "
        "const fields = (end < 0 ? saved : saved.slice(0, end)).trimEnd().split('#'); "
        "const name = fields[0] || 'CloudDrive2'; "
        "const input = event.target.value.replace(/[\\r\\n#]/g, '').trim(); "
    )
    return _group(
        _field("服务器地址", _node("input", {
            "type": "text", "value": _expr(f"{_FIRST}[1] || ''"), "placeholder": "http://nas.local:19798",
            "onInput": (
                "(event) => { " + parse +
                "cd2_confs = [name, input, ...(fields.length > 2 ? fields.slice(2) : [''])].join('#') + tail; }"
            ),
        })),
        _field("API 令牌", _node("input", {
            "type": "password", "autocomplete": "new-password",
            "placeholder": _expr(f"{_FIRST}.length >= 3 && {_FIRST}[2] ? '已保存，留空沿用' : '输入令牌'"),
            "onChange": (
                "(event) => { " + parse +
                "if (input) cd2_confs = [name, fields[1] || '', input].join('#') + tail; "
                "event.target.value = ''; }"
            ),
        })),
    )


def build_settings_form(message_types, upload_limit):
    mode = _select(
        "外网播放时", "playback_mode", [("pause", "暂停上传与复制"), ("limit", "限制上传速度")],
        handler=(
            "(event) => { playback_mode = event.target.value; "
            "if (playback_mode === 'pause') playback_upload_limit = 0; "
            "else if (!(Number(playback_upload_limit) > 0)) playback_upload_limit = 200; }"
        ),
    )
    advanced = _node(
        "details", {"class": "cd2-details"},
        _text("summary", "高级设置"),
        _group(
            _input("播放检查间隔（秒）", "playback_interval", type="number", min=5, max=300),
            _input("健康检查周期", "cron", placeholder="*/10 * * * *"),
            _select("通知消息类型", "msgtype", [(item.name, item.value) for item in message_types]),
            _input("播放来源例外", "playback_proxy_sources", placeholder="代理 IP 或 CIDR，选填"),
            _input("跳过健康检查的目录", "black_dir", placeholder="多个目录用逗号分隔"),
            _node("div", {"class": "cd2-tools"},
                  _switch("onlyonce", "立即检查一次"), _switch("cd2_restart", "重启 CloudDrive2 一次")),
        ),
    )
    form = _node(
        "VForm", {"class": "cd2-settings", "onSubmit": "(event) => event.preventDefault()"},
        _text("style", _STYLE),
        _node(
            "header", {"class": "cd2-header cd2-bar"},
            _node("div", {"class": "cd2-title"},
                  _text("span", "☁︎", **{"class": "cd2-emblem", "aria-hidden": "true"}),
                  _node("div", None, _text("h2", "CloudDrive2助手"), _text("p", "插件设置", **{"class": "cd2-note"}))),
            _switch("enabled", "启用插件"),
        ),
        _node(
            "div", {"class": "cd2-body"},
            _node("section", {"class": "cd2-section"},
                  _node("div", {"class": "cd2-bar"}, _text("h3", "外网播放保护"),
                        _switch("playback_pause", "开启外网播放保护", icon_only=True)),
                  _group(mode,
                         _input("播放结束后恢复（秒）", "playback_resume_delay", type="number", min=0, max=3600),
                         _input("上传上限（KB/s）", "playback_upload_limit", type="number", min=1, max=1000000,
                                show="playback_mode === 'limit'"),
                         show="playback_pause"),
                  _text("p", "忽略内网播放，只恢复插件暂停的任务。", **{"class": "cd2-note cd2-hint"})),
            _node("section", {"class": "cd2-section"}, _text("h3", "CloudDrive2 连接"), _connection_fields()),
            _node("section", {"class": "cd2-section cd2-bar"},
                  _node("div", None, _text("h3", "异常通知"),
                        _text("p", "通过 MoviePilot 已配置的渠道发送", **{"class": "cd2-note"})),
                  _switch("notify", "开启异常通知", icon_only=True)),
            advanced,
        ),
    )
    return [form], {
        "enabled": False,
        "notify": False,
        "onlyonce": False,
        "cd2_restart": False,
        "cron": "*/10 * * * *",
        "cd2_confs": "",
        "msgtype": "Manual",
        "black_dir": "",
        "playback_pause": False,
        "playback_mode": "limit" if upload_limit > 0 else "pause",
        "playback_interval": 10,
        "playback_resume_delay": 45,
        "playback_upload_limit": upload_limit,
        "playback_proxy_sources": "",
    }
