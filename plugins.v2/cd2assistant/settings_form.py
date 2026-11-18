"""Native MoviePilot settings, shared by the V2 and V3 plugin packages."""


def _component(name, props=None, *children):
    return {"component": name, "props": props or {}, "content": list(children)}


def _heading(text):
    return {"component": "h3", "props": {"class": "text-subtitle-2 mb-1"}, "text": text}


def _field(component, label, model=None, *, md=6, show=None, **props):
    column = {"cols": 12, "md": md}
    if show is not None:
        column["show"] = "{{ " + show + " }}"
    if model is not None:
        props["model"] = model
    return _component("VCol", column, _component(component, {"label": label, "density": "compact", **props}))


def _row(*fields):
    return _component("VRow", {"class": "mt-0"}, *fields)


def _accordion(title, *children):
    return _component(
        "VExpansionPanels", {"variant": "accordion", "class": "mt-4"},
        _component("VExpansionPanel", {"title": title}, _component("VExpansionPanelText", None, *children)),
    )


def _single_connection_field(index, label, simple, value, parts, **props):
    # Edit the original field so saving a different setting never rewrites credentials or an instance name.
    other = 3 - index
    return _field("VTextField", label, show=simple, **{
        "modelValue": "{{ " + parts + f"[{index}] || ''" + " }}",
        "onUpdate:modelValue": (
            f"(input) => {{ const previous = {value}.split('#'); "
            "const name = previous[0] || 'CloudDrive2'; "
            f"const next = [name, previous[1] || '', previous[2] || '']; "
            f"next[{index}] = String(input ?? '').replace(/[\\r\\n#]/g, '').trim(); "
            f"cd2_confs = next[{index}] || next[{other}] ? next.join('#') : ''; }}"
        ),
        **props,
    })


def build_settings_form(message_types, upload_limit):
    value = "(typeof cd2_confs === 'string' ? cd2_confs.trim() : '')"
    parts = value + ".split('#')"
    simple = f"!{value} || (!/[\\r\\n]/.test({value}) && {parts}.length === 3)"
    mode_options = [
        {"title": "暂停上传与复制", "value": "pause"},
        {"title": "限制上传速度", "value": "limit"},
    ]
    mode_handler = (
        "(mode) => { playback_mode = mode; "
        "if (mode === 'pause') playback_upload_limit = 0; "
        "else if (!(Number(playback_upload_limit) > 0)) playback_upload_limit = 200; }"
    )
    advanced = _accordion(
        "高级设置",
        _row(
            _field("VTextField", "播放检查间隔（秒）", "playback_interval", type="number", min=5, max=300),
            _field("VCronField", "健康检查周期", "cron", placeholder="*/10 * * * *"),
            _field("VSelect", "通知消息类型", "msgtype",
                   items=[{"title": item.value, "value": item.name} for item in message_types]),
            _field("VTextField", "跳过健康检查的目录", "black_dir", placeholder="多个目录用逗号分隔"),
            _field("VTextField", "视为外网的代理来源 IP/CIDR", "playback_proxy_sources",
                   md=12, placeholder="通常留空，多个来源用逗号分隔"),
            _field("VSwitch", "立即检查一次", "onlyonce", **{"hide-details": True}),
            _field("VSwitch", "重启 CloudDrive2 一次", "cd2_restart", **{"hide-details": True}),
        ),
        _accordion(
            "多台连接或旧版配置",
            _row(_field("VTextarea", "连接列表", "cd2_confs", md=12, rows=2,
                        hint="一行一台，填 名称#地址#API令牌，或 名称#地址#用户名#密码。")),
        ),
    )

    form = _component(
        "VForm", None,
        _row(_field("VSwitch", "启用插件", "enabled", **{"hide-details": True})),
        _component("VDivider", {"class": "my-3"}),
        _heading("外网播放保护"),
        _row(
            _field("VSwitch", "启用保护（忽略内网播放）", "playback_pause", md=12, **{"hide-details": True}),
            _field("VSelect", "外网播放时", show="playback_pause", items=mode_options, **{
                "modelValue": "{{ playback_mode }}", "onUpdate:modelValue": mode_handler,
            }),
            _field("VTextField", "播放结束后恢复（秒）", "playback_resume_delay",
                   show="playback_pause", type="number", min=0, max=3600),
            _field("VTextField", "上传上限（KB/s）", "playback_upload_limit",
                   show="playback_pause && playback_mode === 'limit'", type="number", min=1, max=1000000,
                   hint="API 令牌需能读取和修改系统设置。"),
        ),
        _component("VDivider", {"class": "my-3"}),
        _heading("CloudDrive2 连接"),
        _row(
            _single_connection_field(1, "服务器地址", simple, value, parts, placeholder="http://nas.local:19798"),
            _single_connection_field(2, "API 令牌", simple, value, parts, type="password", autocomplete="off"),
        ),
        _component("VAlert", {"show": "{{ !(" + simple + ") }}", "variant": "tonal", "density": "compact",
                              "text": "已有多台连接或旧版账号配置。请到高级设置修改。"}),
        _component("VDivider", {"class": "my-3"}),
        _heading("异常通知"),
        _row(_field("VSwitch", "通过 MoviePilot 通知", "notify", **{"hide-details": True})),
        advanced,
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
