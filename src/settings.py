"""Native settings: models require a restart; conversation changes apply on save."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

import builtin
import judge
import userconfig
import settings_config as config
import ui_style


PALETTE = ui_style.PALETTE


class SettingsController(NSObject):
    @objc.python_method
    def build(self, hud=None):
        self.hud = hud
        self.path = userconfig.env_files()[0]
        self.original = config.read_document(self.path)
        values = userconfig.parse_env_file(self.path)
        self.file_values = values
        self.initial = {}
        self.fields = {}
        self.controls = []
        self.busy = False
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 760, 648),
            A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
        self.window.setTitle_("设置 · 模型与会话上下文")
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(A.NSColor.clearColor())
        self.window.setHasShadow_(True)
        # The HUD and OCR overlay float above normal windows; settings must sit above both.
        self.window.setLevel_(A.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = A.NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 648))
        view.setMaterial_(getattr(
            A, "NSVisualEffectMaterialSidebar",
            getattr(A, "NSVisualEffectMaterialLight", 1)))
        view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        view.setState_(A.NSVisualEffectStateActive)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.window.setContentView_(view)

        title = self.label(view, "模型与会话设置", 24, 598, 710, 28, 22)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(22))
        title.setTextColor_(PALETTE["text"])
        self.label(view, "编辑文件：" + str(self.path).replace(str(Path.home()), "~"),
                   24, 570, 710, 20, 11, PALETTE["muted"])

        restart_box = ui_style.make_surface(
            10, PALETTE["amber"].colorWithAlphaComponent_(0.10),
            PALETTE["amber"].colorWithAlphaComponent_(0.18))
        restart_box.setFrame_(NSMakeRect(24, 530, 710, 34))
        view.addSubview_(restart_box)
        restart_notice = self.label(view, "模型配置保存后重启 · 会话设置保存后立即生效",
                                    38, 536, 680, 20, 13, PALETTE["amber"])
        restart_notice.setFont_(A.NSFont.boldSystemFontOfSize_(13))

        tab_surface = ui_style.make_surface(14, PALETTE["surface"], PALETTE["edge"])
        tab_surface.setFrame_(NSMakeRect(16, 176, 728, 342))
        view.addSubview_(tab_surface)
        self.tabs = A.NSTabView.alloc().initWithFrame_(NSMakeRect(24, 184, 712, 326))
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
        self.build_context_tab()
        view.addSubview_(self.tabs)
        # #38: 离线判断模型管理。删除是显式确认动作；「启用」只写选择，真正的
        # 下载发生在下次启动的预热——设置窗口里不藏一个 7 GB 的下载按钮。
        offline_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
        offline_surface.setFrame_(NSMakeRect(24, 126, 710, 44))
        view.addSubview_(offline_surface)
        self.offline_label = self.label(view, "", 36, 140, 540, 20, 11, PALETTE["text"])
        self.offline_delete_btn = self.button(view, "删除模型…", "deleteOfflineModel:",
                                              596, 132, 118)
        self.offline_enable_btn = self.button(view, "启用离线判断…", "enableOfflineModel:",
                                              596, 132, 118)
        self.controls.append(self.offline_delete_btn)
        self.controls.append(self.offline_enable_btn)
        self.refresh_offline_section()
        priority_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
        priority_surface.setFrame_(NSMakeRect(24, 74, 710, 44))
        view.addSubview_(priority_surface)
        self.label(view, "优先级：环境变量 > 用户 env > 项目 .env > 内置；两组生成密钥同时存在时 OpenAI 优先。\n清空此文件的密钥不屏蔽其他来源；切换服务需清除原来源中的优先密钥。", 36, 80, 686, 32, 11, PALETTE["muted"])
        self.status = self.label(view, "测试会发送固定问候语，不读取微信内容；可能产生少量服务费用。", 24, 26, 550, 38, 11, PALETTE["muted"])
        self.set_status(self.status.stringValue())
        self.save_button = self.button(view, "保存配置", "saveSettings:", 602, 29, 132, True)
        self.controls.append(self.save_button)
        self.window.center()
        self.refresh_contexts()
        return self

    @objc.python_method
    def build_context_tab(self):
        import chat_context
        item = A.NSTabViewItem.alloc().initWithIdentifier_("CONTEXT")
        item.setLabel_("会话记录与背景")
        panel = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 690, 300))
        group = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
        group.setFrame_(NSMakeRect(12, 168, 666, 120))
        panel.addSubview_(group)
        title = self.label(panel, "记录聊天历史", 26, 254, 490, 20, 13)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(13))
        self.label(panel, "每个会话保留最近 100 条，关闭后保留已有记录", 26, 235, 530, 18, 11, PALETTE["muted"])
        self.history_switch = A.NSSwitch.alloc().initWithFrame_(NSMakeRect(610, 242, 46, 28))
        self.history_switch.setAccessibilityLabel_("记录聊天历史")
        self.history_switch.setTarget_(self)
        self.history_switch.setAction_("contextControlChanged:")
        enabled = self.hud.history_enabled if self.hud else userconfig.get("JEV_HISTORY") == "1"
        self.history_switch.setState_(A.NSOnState if enabled else A.NSOffState)
        panel.addSubview_(self.history_switch)
        divider = ui_style.make_surface(0, PALETTE["edge"])
        divider.setFrame_(NSMakeRect(26, 228, 638, 1))
        panel.addSubview_(divider)
        title = self.label(panel, "模型参考条数", 26, 204, 490, 20, 13)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(13))
        self.label(panel, "使用最近的消息，包含当前消息（1—100 条）", 26, 185, 530, 18, 11, PALETTE["muted"])
        self.context_count = A.NSTextField.alloc().initWithFrame_(NSMakeRect(580, 194, 56, 28))
        try:
            count = self.hud.context_limit if self.hud else chat_context.message_limit(
                userconfig.get("JEV_CONTEXT_MESSAGES") or "20")
        except ValueError:
            count = 20
        self.context_count.setStringValue_(str(count))
        self.style_field(self.context_count)
        self.context_count.setAlignment_(A.NSTextAlignmentCenter)
        self.context_count.setDelegate_(self)
        self.context_count.setAccessibilityLabel_("模型参考条数，1 到 100")
        panel.addSubview_(self.context_count)
        self.label(panel, "条", 642, 197, 22, 20, 11, PALETTE["muted"])
        self.initial.update({"JEV_HISTORY": "1" if enabled else "0", "JEV_CONTEXT_MESSAGES": str(count)})
        for key, control in (("JEV_HISTORY", self.history_switch),
                             ("JEV_CONTEXT_MESSAGES", self.context_count)):
            if userconfig.source_of(key) == "环境变量":
                control.setEnabled_(False)
                control.setToolTip_("由启动环境变量控制；修改环境变量后重启。")
        title = self.label(panel, "聊天背景", 18, 138, 300, 22, 13)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(13))
        self.context_picker = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(428, 136, 244, 28), False)
        self.context_picker.setFont_(A.NSFont.systemFontOfSize_(12))
        self.context_picker.setAccessibilityLabel_("选择要编辑背景的会话")
        self.context_picker.setTarget_(self)
        self.context_picker.setAction_("selectContext:")
        panel.addSubview_(self.context_picker)
        scroll = A.NSScrollView.alloc().initWithFrame_(NSMakeRect(18, 34, 650, 94))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(A.NSNoBorder)
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(ui_style.RADIUS_FIELD)
        scroll.layer().setBorderColor_(PALETTE["edge"].CGColor())
        scroll.layer().setBorderWidth_(0.75)
        scroll.layer().setMasksToBounds_(True)
        self.background_editor = A.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 630, 94))
        self.background_editor.setRichText_(False)
        self.background_editor.setFont_(A.NSFont.systemFontOfSize_(12))
        self.background_editor.setTextColor_(PALETTE["text"])
        self.background_editor.setBackgroundColor_(PALETTE["field"])
        self.background_editor.setTextContainerInset_((10, 8))
        self.background_editor.setAccessibilityLabel_("所选会话背景，可输入多行，清空后保存可删除背景")
        self.background_editor.setToolTip_("例如：AAA 是群主，BBB 是公司老板。背景不占消息条数；清空后保存可删除背景。")
        self.background_editor.setDelegate_(self)
        self.context_title = ""
        self.background_initial = {}
        self.background_drafts = {}
        scroll.setDocumentView_(self.background_editor)
        panel.addSubview_(scroll)
        self.label(panel, "记录在本机保存，推理时发送给所选模型服务。", 18, 5, 500, 18, 10, PALETTE["muted"])
        self.history_menu = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(546, 0, 126, 28), True)
        self.history_menu.setFont_(A.NSFont.systemFontOfSize_(11))
        self.history_menu.addItemsWithTitles_(["管理记录", "清空所选会话记录", "清空全部记录"])
        self.history_menu.menu().setAutoenablesItems_(False)
        self.history_menu.setToolTip_("清空立即生效，保留聊天背景。")
        for index, action in enumerate(("clearCurrentHistory:", "clearAllHistory:"), 1):
            entry = self.history_menu.itemAtIndex_(index)
            entry.setTarget_(self)
            entry.setAction_(action)
        panel.addSubview_(self.history_menu)
        item.setView_(panel)
        self.tabs.addTabViewItem_(item)

    @objc.python_method
    def refresh_contexts(self):
        drafts = self.changed_backgrounds()
        store = self.hud.reload_conversations() if self.hud else None
        data = store.data if store is not None else {}
        current = ((self.hud._last_full or {}).get("chat_title") or "") if self.hud else ""
        drafts = {title: text for title, text in drafts.items()
                  if text != data.get(title, {}).get('background', '')}
        titles = sorted(set(data) | set(drafts) | ({current} if current else set())) if store is not None else []
        selected = self.context_title if self.context_title in titles else current
        self.background_initial = {title: data.get(title, {}).get('background', '') for title in titles}
        self.background_drafts = drafts
        # Untouched cached text is not a draft and must not revive an externally deleted background.
        self.context_title = ""
        self.context_picker.removeAllItems()
        self.context_picker.addItemsWithTitles_(titles or ["暂无已保存的会话"])
        if selected in titles:
            self.context_picker.selectItemWithTitle_(selected)
        self.context_picker.setEnabled_(bool(titles))
        self.background_editor.setEditable_(bool(titles))
        self.selectContext_(self.context_picker)
        self.history_menu.setEnabled_(store is not None and not store.error)
        self.history_menu.itemAtIndex_(1).setEnabled_(bool(titles))
        self.history_menu.itemAtIndex_(2).setEnabled_(store is not None)
        error = store.error if store is not None else ""
        if error:
            self.set_status(error, "error")
        elif getattr(self, '_context_error', ''):
            self.set_status("本地会话文件已恢复。" + ("背景草稿仍需点击「保存配置」。" if drafts else ""))
        self._context_error = error

    def windowDidBecomeKey_(self, notification):
        self.refresh_contexts()

    def selectContext_(self, sender):
        if self.context_title:
            self.background_drafts[self.context_title] = str(self.background_editor.string())
        self.context_title = str(sender.titleOfSelectedItem()) if sender.isEnabled() else ""
        self.background_editor.setString_(self.background_drafts.get(
            self.context_title, self.background_initial.get(self.context_title, "")))

    @objc.python_method
    def changed_backgrounds(self):
        if self.context_title:
            self.background_drafts[self.context_title] = str(self.background_editor.string())
        return {title: text for title, text in self.background_drafts.items()
                if text != self.background_initial[title]}

    def contextControlChanged_(self, sender):
        self.set_status("会话记录与背景已修改，点击「保存配置」后生效。")

    def textDidChange_(self, notification):
        self.contextControlChanged_(None)

    def clearCurrentHistory_(self, sender):
        self.context_action("current")

    def clearAllHistory_(self, sender):
        self.context_action("all")

    @objc.python_method
    def context_action(self, action):
        try:
            self.hud.clear_history(self.context_title if action == "current" else None)
        except (OSError, ValueError):
            self.set_status((self.hud.conversations.error if self.hud.conversations else "")
                            or "保存失败，请检查本地数据权限及可用磁盘空间。", "error")
            return
        self.refresh_contexts()
        self.set_status("记录已清空，背景保留；后续有效读屏按开关继续记录。", "success")

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
    def refresh_offline_section(self):
        cached = judge.model_cached()
        if cached:
            text = f"离线判断模型：已下载（{judge.model_disk_usage() / 1e9:.1f} GB 磁盘占用）"
            if userconfig.get("JUDGE_BACKEND").strip().lower() == "cloud":
                text += " · 当前选择在线判断"
        else:
            text = "离线判断模型：未下载 · 启用后下次启动预热时下载（约 3.8 GB）"
        self.offline_label.setStringValue_(text)
        self.offline_delete_btn.setHidden_(not cached)
        self.offline_enable_btn.setHidden_(cached)

    def deleteOfflineModel_(self, sender):
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_("删除离线判断模型？")
        alert.setInformativeText_("之后使用离线判断需重新下载（约 3.8 GB）。正在运行的应用不受影响，重启后生效。")
        alert.addButtonWithTitle_("删除")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != A.NSAlertFirstButtonReturn:
            return
        sender.setEnabled_(False)
        self.set_status("正在删除离线判断模型…")
        threading.Thread(target=self._delete_model_work, daemon=True).start()

    @objc.python_method
    def _delete_model_work(self):
        import shutil
        error = ""
        try:
            shutil.rmtree(judge.model_cache_dir())
        except OSError as e:
            error = str(e)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "modelDeleted:", error, False)

    def modelDeleted_(self, error):
        self.refresh_offline_section()
        if error:
            self.set_status(f"删除失败：{error[:80]}", "error")
        else:
            self.set_status("已删除离线判断模型。正在运行的判断不受影响；删除的文件不可恢复。", "success")

    def enableOfflineModel_(self, sender):
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_("启用离线判断？")
        alert.setInformativeText_("下次启动的预热将下载判断模型（约 3.8 GB，一次性），之后判断完全离线进行。")
        alert.addButtonWithTitle_("启用")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != A.NSAlertFirstButtonReturn:
            return
        try:
            self.original = config.write_settings(self.path, self.original,
                                                  {"JUDGE_BACKEND": "local"})
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.file_values["JUDGE_BACKEND"] = "local"
        self.refresh_offline_section()
        self.set_status("已启用离线判断（写入 JUDGE_BACKEND=local）。请退出并重新打开应用，预热时开始下载。",
                        "success")

    @objc.python_method
    def show(self):
        self.refresh_contexts()
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def values(self, prefix):
        return {k: str(v.stringValue()) for k, v in self.fields[prefix].items()}

    @objc.python_method
    def changed(self):
        values = {f"{p}_{k}": v for p in config.PREFIXES for k, v in self.values(p).items()}
        if self.history_switch.isEnabled():
            values["JEV_HISTORY"] = "1" if self.history_switch.state() == A.NSOnState else "0"
        if self.context_count.isEnabled():
            values["JEV_CONTEXT_MESSAGES"] = str(self.context_count.stringValue()).strip()
        return {k: v for k, v in values.items() if v != self.initial[k]}

    def controlTextDidChange_(self, notification):
        field = notification.object()
        if field == self.context_count:
            self.contextControlChanged_(field)
            return
        for fields in self.fields.values():
            if field in (fields["API_KEY"], fields["BASE_URL"]):
                combo = fields["MODEL"]
                self.set_models(combo, [])
        self.set_status("配置已修改，请重新测试；保存后重启生效。")

    def saveSettings_(self, sender):
        from chat_context import message_limit
        self.window.makeFirstResponder_(None)
        self.refresh_contexts()
        changes = self.changed()
        backgrounds = self.changed_backgrounds()
        if not changes and not backgrounds:
            if not self._context_error:
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
            if changes:
                self.original = config.write_settings(self.path, self.original, changes)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.initial.update(changes)
        self.file_values.update(changes)
        context_changes = {k: v for k, v in changes.items() if k in ("JEV_HISTORY", "JEV_CONTEXT_MESSAGES")}
        for key, value in context_changes.items():
            userconfig.session_override(key, value)
        if context_changes and self.hud:
            try:
                effective_count = message_limit(userconfig.get("JEV_CONTEXT_MESSAGES") or "20")
            except ValueError:
                effective_count = 20
            self.hud.configure_context(userconfig.get("JEV_HISTORY") == "1", str(effective_count))
        saved = bool(changes)
        try:
            for title, text in backgrounds.items():
                self.hud.save_background(title, text)
                self.background_initial[title] = text
                saved = True
        except (ValueError, OSError):
            self.set_status(("部分配置已保存；" if saved else "保存失败；")
                            + ((self.hud.conversations.error if self.hud.conversations else "")
                               or "请检查本地数据权限或磁盘空间后重试。") + "背景草稿仍保留。", "error")
            return
        model_changes = changes.keys() - context_changes.keys()
        if model_changes and not (context_changes or backgrounds):
            self.set_status("已保存。请退出并重新打开应用；当前会话继续使用启动时的配置。", "success")
        else:
            self.set_status("会话记录与背景已保存并生效。" + ("模型配置需退出并重新打开应用。" if model_changes else ""), "success")

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
        if self.changed() or self.changed_backgrounds():
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
