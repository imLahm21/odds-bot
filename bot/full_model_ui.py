"""HTML/inline-keyboard UI for full-model profiles; Telegram auth stays in tgbot."""

from html import escape

from . import config, db, full_model_settings as settings


_PROFILES = {"a": "analyze", "r": "review"}
_SLOTS = ("主模型", "第一回退", "第二回退")


def _button(text, data):
    return {"text": text, "callback_data": data}


def _screen(text, rows, notice=""):
    return text, {"inline_keyboard": rows}, notice


def _index(raw, sequence):
    if not raw.isascii() or not raw.isdecimal() or not 0 <= int(raw) < len(sequence):
        raise ValueError("配置按钮已失效或参数越界")
    return int(raw)


def _summary(pair):
    if not pair["model"]:
        return "— 未设"
    return f"<code>{escape(pair['model'])}</code> / {escape(pair['effort'] or '模型默认')}"


def _profile_panel(conn, code, notice=""):
    profile = _PROFILES[code]
    roles = db.get_full_model_profile(conn, profile)
    lines = [f"🎛️ <b>{settings.PROFILES[profile]} · 模型配置</b>",
             "精算与复盘独立保存；点击职责调整主模型、两个回退及各自强度。", ""]
    rows = []
    for index, spec in enumerate(roles.values()):
        lines.append(f"{index + 1}. {escape(spec['label'])}：{_summary(spec)}")
        rows.append([_button(spec["label"], f"fmc:r:{code}:{index}")])
    rescue = db.get_full_model_rescue(conn, profile)
    lines += ["", "🛟 公共兜底：" + _summary(rescue)
              + (f" · <code>{escape(rescue['route_group'])}</code>" if rescue["model"] else "（已关闭）")]
    rows.append([_button("🛟 公共兜底模型 / 授权组 / 强度", f"fmc:g:{code}")])
    lines += ["", "修改用于下一次任务；已开始的任务保持原配置。",
              "Jev 是独立决策层，不使用这里的推理强度设置。"]
    rows += [[_button("↩️ 恢复本配置默认", f"fmc:z:{code}")],
             [_button("↩️ 返回全模型设置", "fmc:")]]
    return _screen("\n".join(lines), rows, notice)


def _role_panel(conn, code, index, notice=""):
    profile = _PROFILES[code]
    spec = list(db.get_full_model_profile(conn, profile).values())[index]
    lines = [f"🎚️ <b>{settings.PROFILES[profile]} · {escape(spec['label'])}</b>", ""]
    rows = []
    for slot, pair in enumerate(settings.slots(spec)):
        lines.append(f"{_SLOTS[slot]}：{_summary(pair)}")
        rows.append([_button(f"✏️ {_SLOTS[slot]}", f"fmc:m:{code}:{index}:{slot}"),
                     _button("🧠 推理强度", f"fmc:e:{code}:{index}:{slot}")])
    rows += [[_button("清空第一回退", f"fmc:x:{code}:{index}:1"),
              _button("清空第二回退", f"fmc:x:{code}:{index}:2")],
             [_button("↩️ 恢复本职责默认", f"fmc:d:{code}:{index}")],
             [_button("↩️ 返回职责列表", f"fmc:p:{code}")]]
    lines += ["", "回退须与主模型同等级，第一有效回退须跨授权组。",
              "更换模型后选择强度，才会一并保存。接口可用性请另做密钥组测试。"]
    return _screen("\n".join(lines), rows, notice)


def _effort_panel(code, index, slot, model_index):
    model = list(config.LLM_MODELS)[model_index]
    settings.require_model(model)
    efforts = ("", *config.llm_model_efforts(model))
    rows = [[_button(config.LLM_EFFORT_LABELS.get(effort, "模型默认（不发送参数）"),
                     f"fmc:s:{code}:{index}:{slot}:{model_index}:{i}")]
            for i, effort in enumerate(efforts)]
    rows.append([_button("↩️ 返回职责", f"fmc:r:{code}:{index}")])
    text = (f"🧠 <b>{_SLOTS[slot]} · 推理强度</b>\n"
            f"模型：<code>{escape(model)}</code>\n"
            "点击强度后保存；只列本地注册表允许的参数，不代表上游已实测。")
    if model.endswith("-high"):
        text += "\nCPA 的 -high 是模型名的一部分，独立强度参数是否生效仍须实测。"
    return _screen(text, rows)


def _rescue_panel(conn, code, notice=""):
    pair = db.get_full_model_rescue(conn, _PROFILES[code])
    text = (f"🛟 <b>{settings.PROFILES[_PROFILES[code]]} · 公共兜底</b>\n"
            f"模型 / 强度：{_summary(pair)}\n"
            f"授权组：<code>{escape(pair['route_group'] or '—')}</code>\n\n"
            "仅在职责常规链耗尽后追加一轮；严格限定所选授权组，不轮转到其他组。\n"
            "同样保留防自审与部分流式正文不重生成的保护。兜底仍可能失败。")
    return _screen(text, [
        [_button("✏️ 兜底模型", f"fmc:gm:{code}")],
        [_button("🔐 授权组", f"fmc:gr:{code}"), _button("🧠 强度", f"fmc:ge:{code}")],
        [_button("关闭公共兜底", f"fmc:gx:{code}")],
        [_button("↩️ 恢复兜底默认", f"fmc:gz:{code}")],
        [_button("↩️ 返回职责列表", f"fmc:p:{code}")],
    ], notice)


def _rescue_effort_panel(code, model_index, group_index):
    model = list(config.LLM_MODELS)[model_index]
    settings.require_model(model)
    group = config.llm_route_groups_for_model(model)[group_index]
    efforts = ("", *config.llm_model_efforts(model))
    rows = [[_button(config.LLM_EFFORT_LABELS.get(effort, "模型默认（不发送参数）"),
                     f"fmc:gs:{code}:{model_index}:{group_index}:{index}")]
            for index, effort in enumerate(efforts)]
    rows.append([_button("↩️ 返回公共兜底", f"fmc:g:{code}")])
    return _screen(f"🧠 <b>公共兜底 · 选择强度后保存</b>\n"
                   f"模型：<code>{escape(model)}</code>\n授权组：<code>{escape(group)}</code>\n"
                   "仅列本地登记的参数，不保证上游接受或落实该强度。", rows)


def _rescue_groups(code, model_index):
    model = list(config.LLM_MODELS)[model_index]
    settings.require_model(model)
    groups = config.llm_route_groups_for_model(model)
    if len(groups) == 1:
        return _rescue_effort_panel(code, model_index, 0)
    return _screen(f"🔐 为 <code>{escape(model)}</code> 选择公共兜底的固定授权组：", [
        [_button(config.LLM_ROUTE_GROUPS[group]["label"] + f" · {group}",
                 f"fmc:gg:{code}:{model_index}:{index}")] for index, group in enumerate(groups)
    ] + [[_button("↩️ 返回公共兜底", f"fmc:g:{code}")]])


def _handle_rescue(conn, code, action, parts):
    profile = _PROFILES[code]
    pair = db.get_full_model_rescue(conn, profile)
    models = list(config.LLM_MODELS)
    if action == "g":
        return _rescue_panel(conn, code)
    if action == "gm":
        return _screen("请选择公共兜底模型，再选择授权组和强度完成保存。", [
            [_button(config.llm_model_label(model), f"fmc:gc:{code}:{index}")]
            for index, model in enumerate(models) if settings.model_allowed(model)
        ] + [[_button("↩️ 返回公共兜底", f"fmc:g:{code}")]])
    if action == "gz":
        return _screen("确认将本配置的公共兜底恢复为代码默认？不会更改职责链或另一配置。", [
            [_button("确认恢复", f"fmc:gzd:{code}")],
            [_button("取消", f"fmc:g:{code}")]])
    if action == "gzd":
        db.reset_full_model_rescue(conn, profile)
        return _rescue_panel(conn, code, "已恢复兜底默认")
    if action == "gx":
        ok, error = db.set_full_model_rescue(conn, profile, "", "", "")
    else:
        if action in ("ge", "gr"):
            if not pair["model"]:
                raise ValueError("请先选择兜底模型")
            model_index = models.index(pair["model"])
            if action == "gr":
                return _rescue_groups(code, model_index)
            group_index = config.llm_route_groups_for_model(pair["model"]).index(pair["route_group"])
            return _rescue_effort_panel(code, model_index, group_index)
        model_index = _index(parts[3], models)
        if action == "gc":
            return _rescue_groups(code, model_index)
        groups = config.llm_route_groups_for_model(models[model_index])
        group_index = _index(parts[4], groups)
        if action == "gg":
            return _rescue_effort_panel(code, model_index, group_index)
        efforts = ("", *config.llm_model_efforts(models[model_index]))
        effort_index = _index(parts[5], efforts)
        ok, error = db.set_full_model_rescue(
            conn, profile, models[model_index], efforts[effort_index], groups[group_index])
    if not ok:
        raise ValueError(error)
    return _rescue_panel(conn, code, "已保存，下次任务生效")


def handle_callback(conn, data: str):
    """Return (HTML, keyboard, toast), or reject malformed/invalid edits."""
    parts = data.split(":")
    if parts == ["fmc", ""]:
        return _screen("🎛️ <b>全模型设置</b>\n两套配置独立保存，不改变普通三档模型。", [
            [_button("🎯 全模型交叉精算", "fmc:p:a")],
            [_button("🔍 全模型交叉复盘", "fmc:p:r")],
            [_button("↩️ 返回 LLM 面板", "lm:")],
        ])
    arity = {"p": 3, "r": 4, "m": 5, "e": 5, "c": 6, "s": 7,
             "x": 5, "z": 3, "zd": 3, "d": 4, "dd": 4,
             "g": 3, "gm": 3, "ge": 3, "gr": 3, "gc": 4, "gg": 5, "gs": 6,
             "gx": 3, "gz": 3, "gzd": 3}
    if (len(parts) < 3 or parts[0] != "fmc" or parts[1] not in arity
            or len(parts) != arity[parts[1]] or parts[2] not in _PROFILES):
        raise ValueError("全模型配置按钮无效")
    action, code = parts[1:3]
    profile = _PROFILES[code]
    if action.startswith("g"):
        return _handle_rescue(conn, code, action, parts)
    if action == "p":
        return _profile_panel(conn, code)
    if action == "z":
        return _screen(f"确认恢复 <b>{settings.PROFILES[profile]}</b> 的全部职责与公共兜底默认？\n"
                       "不会修改另一套配置、普通三档模型或正在运行的任务。", [
            [_button("确认恢复", f"fmc:zd:{code}")],
            [_button("取消", f"fmc:p:{code}")],
        ])
    if action == "zd":
        db.reset_full_model_profile(conn, profile)
        return _profile_panel(conn, code, "已恢复本配置默认")
    role_ids = list(settings.defaults(profile))
    index = _index(parts[3], role_ids)
    role = role_ids[index]
    if action == "r":
        return _role_panel(conn, code, index)
    if action == "d":
        return _screen("确认恢复本职责的主模型、两个回退及各自强度？", [
            [_button("确认恢复", f"fmc:dd:{code}:{index}")],
            [_button("取消", f"fmc:r:{code}:{index}")],
        ])
    if action == "dd":
        db.reset_full_model_profile(conn, profile, role)
        return _role_panel(conn, code, index, "已恢复本职责默认")
    slot = _index(parts[4], _SLOTS)
    spec = db.get_full_model_profile(conn, profile)[role]
    pair = settings.slots(spec)[slot]
    all_models = list(config.LLM_MODELS)
    if action == "m":
        grade = config.FULL_CONSULT_MODEL_GRADES.get(spec["model"])
        rows = []
        for model_index, model in enumerate(all_models):
            if not settings.model_allowed(model):
                continue
            if slot and config.FULL_CONSULT_MODEL_GRADES[model] != grade:
                continue
            mark = "✅ " if model == pair["model"] else ""
            rows.append([_button(mark + config.llm_model_label(model),
                                 f"fmc:c:{code}:{index}:{slot}:{model_index}")])
        rows.append([_button("↩️ 返回职责", f"fmc:r:{code}:{index}")])
        return _screen("请选择模型，再选择强度完成保存。\n"
                       "若主模型切换等级，请先清空两个回退；保存时会校验整条链。", rows)
    if action == "x":
        if slot == 0:
            raise ValueError("不能清空主模型")
        ok, error = db.set_full_model_slot(conn, profile, role, slot, "", "")
    elif action in ("e", "c", "s"):
        if action == "e":
            if not pair["model"]:
                raise ValueError("请先选择回退模型")
            return _effort_panel(code, index, slot, all_models.index(pair["model"]))
        model_index = _index(parts[5], all_models)
        if action == "c":
            return _effort_panel(code, index, slot, model_index)
        model = all_models[model_index]
        efforts = ("", *config.llm_model_efforts(model))
        effort_index = _index(parts[6], efforts)
        ok, error = db.set_full_model_slot(conn, profile, role, slot, model, efforts[effort_index])
    else:
        raise ValueError("未知全模型配置操作")
    if not ok:
        raise ValueError(error)
    return _role_panel(conn, code, index, "已保存，下次任务生效")
