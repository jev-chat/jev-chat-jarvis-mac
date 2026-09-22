"""Native model settings, opened from the HUD menu. Saving requires a restart."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

import builtin
import userconfig
import settings_config as config
import ui_style


PALETTE = ui_style.PALETTE


class SettingsController(NSObject):
    @objc.python_method
    def build(self):
        self.path = userconfig.env_files()[0]
        self.original = config.read_document(self.path)
        values = userconfig.parse_env_file(self.path)
        self.file_values = values
        self.initial = {}
        self.fields = {}
        self.controls = []
        self.busy = False
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 760, 600),
            A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
        self.window.setTitle_("模型设置 · 保存后重启生效")
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(A.NSColor.clearColor())
        self.window.setHasShadow_(True)
        # The HUD and OCR overlay float above normal windows; settings must sit above both.
        self.window.setLevel_(A.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = A.NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 600))
        view.setMaterial_(getattr(
            A, "NSVisualEffectMaterialSidebar",
            getattr(A, "NSVisualEffectMaterialLight", 1)))
        view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        view.setState_(A.NSVisualEffectStateActive)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.window.setContentView_(view)

        title = self.label(view, "模型设置", 24, 550, 710, 28, 22)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(22))
        title.setTextColor_(PALETTE["text"])
        self.label(view, "编辑文件：" + str(self.path).replace(str(Path.home()), "~"),
                   24, 522, 710, 20, 11, PALETTE["muted"])

        restart_box = ui_style.make_surface(
            10, PALETTE["amber"].colorWithAlphaComponent_(0.10),
            PALETTE["amber"].colorWithAlphaComponent_(0.18))
        restart_box.setFrame_(NSMakeRect(24, 482, 710, 34))
        view.addSubview_(restart_box)
        restart_notice = self.label(view, "保存后请退出应用并重启",
                                    38, 488, 680, 20, 13, PALETTE["amber"])
        restart_notice.setFont_(A.NSFont.boldSystemFontOfSize_(13))

        tab_surface = ui_style.make_surface(14, PALETTE["surface"], PALETTE["edge"])
        tab_surface.setFrame_(NSMakeRect(16, 128, 728, 342))
        view.addSubview_(tab_surface)
        self.tabs = A.NSTabView.alloc().initWithFrame_(NSMakeRect(24, 136, 712, 326))
        if hasattr(self.tabs, "setDrawsBackground_"):
            self.tabs.setDrawsBackground_(False)
        titles = ("判断 · Jev", "生成 · OpenAI 兼容", "生成 · Anthropic 兼容")
        for index, (prefix, title) in enumerate(zip(config.PREFIXES, titles)):
            item = A.NSTabViewItem.alloc().initWithIdentifier_(prefix)
            item.setLabel_(title)
            panel = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 690, 300))
            summary, source = self.current_source(prefix)
            source_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
            source_surface.setFrame_(NSMakeRect(12, 212, 666, 76))
            panel.addSubview_(source_surface)
            badge = self.label(panel, summary, 26, 254, 638, 20, 14, PALETTE["green"])
            badge.setFont_(A.NSFont.boldSystemFontOfSize_(14))
            self.label(panel, source, 26, 220, 638, 34, 11, PALETTE["muted"])
            fields = {}
            for name, label, y in (("API_KEY", "密钥", 166), ("BASE_URL", "服务地址", 120), ("MODEL", "模型", 74)):
                row_label = self.label(panel, label, 26, y + 3, 78, 24, 11, PALETTE["text"])
                row_label.setFont_(A.NSFont.boldSystemFontOfSize_(11))
                cls = A.NSSecureTextField if name == "API_KEY" else A.NSComboBox if name == "MODEL" else A.NSTextField
                field = cls.alloc().initWithFrame_(NSMakeRect(112, y, 552, 30))
                default = "" if name == "API_KEY" else config.DEFAULTS[prefix][name == "MODEL"]
                value = values.get(f"{prefix}_{name}", default)
                if name == "API_KEY" and ("$(" in value or "`" in value):
                    value = ""  # Do not evaluate or rewrite shell/keychain expressions.
                    field.setToolTip_("此密钥由 shell 表达式提供；留空保留原行，输入新密钥才会替换。")
                field.setStringValue_(value)
                self.style_field(field)
                field.setDelegate_(self)
                field.setAccessibilityLabel_(title + " " + label)
                if name == "API_KEY":
                    field.setPlaceholderString_("由 shell 表达式提供：留空保留原行，输入新密钥才替换"
                                               if "$(" in values.get(f"{prefix}_{name}", "") or "`" in values.get(f"{prefix}_{name}", "")
                                               else "仅显示此文件中的密钥；不会复制环境变量中的密钥")
                if name == "MODEL":
                    self.set_models(field, [])
                    field.setCompletes_(False)
                    field.setPlaceholderString_("获取模型列表后选择，或手动填写模型名称")
                panel.addSubview_(field)
                fields[name] = field
                self.initial[f"{prefix}_{name}"] = value
                self.controls.append(field)
            self.fields[prefix] = fields
            hint = ("Jev 地址带不带 /v1 都行，网关动作不同时可填完整动作路径；列表接口不可用时可手填模型。" if prefix == "TYPESAFE"
                    else "可手填模型。Ollama 地址通常含 /v1，密钥可填 ollama。" if prefix == "OPENAI"
                    else "使用 Anthropic 消息接口，支持自定义兼容服务地址。")
            self.label(panel, hint, 26, 43, 638, 20, 11, PALETTE["muted"])
            for text, action, x in (("获取模型列表", "fetchModels:", 372), ("测试连接", "testConnection:", 524)):
                button = self.button(panel, text, action, x, 4, 140)
                button.setTag_(index)
                self.controls.append(button)
            item.setView_(panel)
            self.tabs.addTabViewItem_(item)
        view.addSubview_(self.tabs)
        priority_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
        priority_surface.setFrame_(NSMakeRect(24, 74, 710, 44))
        view.addSubview_(priority_surface)
        self.label(view, "优先级：环境变量 > 用户 env > 项目 .env > 内置；两组生成密钥同时存在时 OpenAI 优先。\n清空此文件的密钥不屏蔽其他来源；切换服务需清除原来源中的优先密钥。", 36, 80, 686, 32, 11, PALETTE["muted"])
        self.status = self.label(view, "测试会发送固定问候语，不读取微信内容；可能产生少量服务费用。", 24, 26, 550, 38, 11, PALETTE["muted"])
        self.set_status(self.status.stringValue())
        self.save_button = self.button(view, "保存配置", "saveSettings:", 602, 29, 132, True)
        self.controls.append(self.save_button)
        self.window.center()
        return self

    @objc.python_method
    def set_status(self, text, kind="info"):
        colors = {"info": PALETTE["muted"],
                  "success": PALETTE["green"],
                  "error": PALETTE["red"]}
        self.status.setStringValue_(text)
        self.status.setTextColor_(colors[kind])
        self.status.setFont_(A.NSFont.boldSystemFontOfSize_(11))

    @objc.python_method
    def set_models(self, combo, models):
        current = combo.stringValue()
        combo.removeAllItems()
        combo.addItemsWithObjectValues_(models or ["暂无"])
        combo.setStringValue_(current)

    def comboBoxWillPopUp_(self, notification):
        self.model_before_popup = notification.object().stringValue()

    def comboBoxSelectionDidChange_(self, notification):
        combo = notification.object()
        if list(combo.objectValues()) == ["暂无"]:
            combo.deselectItemAtIndex_(0)
            combo.setStringValue_(getattr(self, "model_before_popup", ""))
        else:
            self.set_status("模型已修改，请重新测试；保存后重启生效。")

    @objc.python_method
    def current_source(self, prefix):
        if prefix == "TYPESAFE":
            source = userconfig.source_of("TYPESAFE_API_KEY", "JEV_API_KEY")
            summary = ("本次启动：正在使用自己的 Jev 密钥" if source != "none"
                       else "本次启动：正在使用本地判断模型，未使用 Jev 密钥")
        else:
            oai = userconfig.provider("OPENAI")
            anth = userconfig.provider("ANTHROPIC")
            selected = "OPENAI" if oai["key"] else "ANTHROPIC" if anth["key"] else None
            if selected:
                name = "OpenAI 兼容" if selected == "OPENAI" else "Anthropic 兼容"
                summary = "本次启动：正在使用自己的密钥（" + name + "）"
                source = (oai if selected == "OPENAI" else anth)["source"]
                if selected != prefix:
                    source += "；本页服务当前未启用"
            else:
                summary = ("本次启动：正在使用内置共享密钥" if builtin.API_KEY
                           else "本次启动：未配置生成密钥")
                source = "应用内置" if builtin.API_KEY else "none"
        detail = "来源：" + source.replace(str(Path.home()), "~") + "\n以下编辑内容保存后，需重启应用才会生效。"
        return summary, detail

    @objc.python_method
    def label(self, view, text, x, y, w, h, size=13, color=None):
        field = ui_style.make_label(text, x, y, w, h, size, color)
        field.cell().setWraps_(True)
        view.addSubview_(field)
        return field

    @objc.python_method
    def style_field(self, field):
        field.setFont_(A.NSFont.systemFontOfSize_(12))
        field.setTextColor_(PALETTE["text"])
        field.setBackgroundColor_(PALETTE["field"])
        field.setWantsLayer_(True)
        field.layer().setBorderColor_(PALETTE["edge"].CGColor())
        field.layer().setBorderWidth_(0.75)
        field.layer().setCornerRadius_(ui_style.RADIUS_FIELD)

    @objc.python_method
    def button(self, view, title, action, x, y, width, primary=False):
        button = A.NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 32))
        button.setTitle_(title)
        ui_style.style_button(button, font_size=11, radius=16, primary=primary)
        button.setTarget_(self)
        button.setAction_(action)
        view.addSubview_(button)
        return button

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def values(self, prefix):
        return {k: str(v.stringValue()) for k, v in self.fields[prefix].items()}

    @objc.python_method
    def changed(self):
        return {f"{p}_{k}": v for p in config.PREFIXES for k, v in self.values(p).items()
                if v != self.initial[f"{p}_{k}"]}

    def controlTextDidChange_(self, notification):
        field = notification.object()
        for fields in self.fields.values():
            if field in (fields["API_KEY"], fields["BASE_URL"]):
                combo = fields["MODEL"]
                self.set_models(combo, [])
        self.set_status("配置已修改，请重新测试；保存后重启生效。")

    def saveSettings_(self, sender):
        self.window.makeFirstResponder_(None)
        changes = self.changed()
        if not changes:
            self.set_status("没有需要保存的修改。")
            return
        # Persist missing displayed defaults for edited services, but keep untouched key lines.
        for prefix in config.PREFIXES:
            if any(k.startswith(prefix + "_") for k in changes):
                changes.update({f"{prefix}_{k}": v for k, v in self.values(prefix).items()
                                if k != "API_KEY" and f"{prefix}_{k}" not in self.file_values})
        try:
            for prefix in config.PREFIXES:
                if any(k.startswith(prefix + "_") for k in changes):
                    vals = self.values(prefix)
                    if vals["API_KEY"] and (not vals["BASE_URL"].strip() or not vals["MODEL"].strip()):
                        raise ValueError("填写密钥后，请同时填写该服务的地址和模型。")
            for key, value in changes.items():
                if key.endswith("_BASE_URL") and value:
                    config.validate_endpoint(value)
            self.original = config.write_settings(self.path, self.original, changes)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.initial.update(changes)
        self.file_values.update(changes)
        self.set_status("已保存。请退出并重新打开应用；当前会话继续使用启动时的配置。", "success")

    def fetchModels_(self, sender):
        self.start_request(sender.tag(), True)

    def testConnection_(self, sender):
        self.start_request(sender.tag(), False)

    @objc.python_method
    def start_request(self, index, listing):
        if self.busy:
            return
        self.window.makeFirstResponder_(None)
        prefix = config.PREFIXES[index]
        values = self.values(prefix)
        try:
            config.validate_endpoint(values["BASE_URL"])
            if not values["API_KEY"]:
                raise ValueError("请填写密钥；Ollama 可填写 ollama。")
            if not listing and not values["MODEL"].strip():
                raise ValueError("请填写模型后再测试。")
            extra = None
            if not listing and prefix == "OPENAI":
                # Match generation's current extra-body setting, without changing it.
                raw = userconfig.get("OPENAI_EXTRA_BODY") or builtin.EXTRA_BODY
                extra = json.loads(raw) if raw else {}
                if not isinstance(extra, dict):
                    raise ValueError("OPENAI_EXTRA_BODY 必须是 JSON 对象。")
        except json.JSONDecodeError:
            self.set_status("OPENAI_EXTRA_BODY 不是有效 JSON，请先修正该配置。", "error")
            return
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        if listing:
            combo = self.fields[prefix]["MODEL"]
            self.set_models(combo, [])
        self.busy = True
        for control in self.controls:
            control.setEnabled_(False)
        self.set_status("正在获取模型列表…" if listing else "正在测试所填服务与模型…")

        def work():
            result = {"index": index, "listing": listing}
            try:
                args = (prefix, values["BASE_URL"], values["API_KEY"])
                if listing:
                    result["models"] = config.list_models(*args)
                else:
                    config.test_connection(*args, values["MODEL"], extra)
            except Exception as e:
                result["error"] = config.error_message(e)
            self.performSelectorOnMainThread_withObject_waitUntilDone_("requestFinished:", result, False)
        threading.Thread(target=work, daemon=True).start()

    def requestFinished_(self, result):
        self.busy = False
        for control in self.controls:
            control.setEnabled_(True)
        if result.get("error"):
            self.set_status(result["error"] + (" 模型仍可手填。" if result["listing"] else ""), "error")
        elif result["listing"]:
            combo = self.fields[config.PREFIXES[result["index"]]]["MODEL"]
            self.set_models(combo, result["models"])
            self.set_status(f"已获取 {len(result['models'])} 个模型。请从下拉列表选择或手填，再测试连接。", "success")
        else:
            self.set_status("连接成功：所填服务与模型返回了有效结果。配置尚需保存并重启生效。", "success")

    def windowShouldClose_(self, sender):
        if self.busy:
            self.set_status("请求进行中，请等待结果后关闭。")
            return False
        if self.changed():
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_("放弃尚未保存的配置？")
            alert.addButtonWithTitle_("继续编辑")
            alert.addButtonWithTitle_("放弃修改")
            return alert.runModal() == A.NSAlertSecondButtonReturn
        return True


if __name__ == "__main__":
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    userconfig.load()
    controller = SettingsController.alloc().init().build()
    controller.show()
    app.run()
