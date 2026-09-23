"""Native model settings, opened from the HUD menu. Saving requires a restart."""
from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

import builtin
import judge
import orcarouter
import userconfig
import settings_config as config
import ui_style


PALETTE = ui_style.PALETTE


class OrcaLoginSession:
    """Owns one PKCE attempt: its generation, its listener, and every way it can end.

    A login that is left running keeps a loopback socket open and leaves the panel busy
    forever, so every terminal path has to release it: success, denial, exchange error,
    timeout, an explicit Cancel, switching to another provider tab, closing the window, and
    app termination.

    A monotonically increasing generation guards every async landing. A late success or a
    late URL from an attempt the user already abandoned must not overwrite the state of the
    attempt that replaced it.

    An AppKit window has no back-forward cache, so there is no `pagehide` event here — but
    the failure mode the spec warns about is real and is handled the same way: `release()`
    clears busy/hint **synchronously in the caller**, and then cancels the server-side work.
    It does not rely on the worker's guarded `finally`, which by design refuses to touch
    state that no longer belongs to it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.generation = 0
        self.busy = False
        self.hint = ""
        self.source: orcarouter.PkceSource | None = None

    def begin(self) -> int:
        with self._lock:
            self.generation += 1
            self.busy = True
            self.hint = "正在准备浏览器授权…"
            return self.generation

    def current(self, generation: int) -> bool:
        with self._lock:
            return self.busy and generation == self.generation

    def set_hint(self, generation: int, text: str) -> None:
        with self._lock:
            if generation == self.generation:
                self.hint = text

    def settle(self, generation: int) -> None:
        with self._lock:
            if generation == self.generation:
                self.busy = False
                self.source = None

    def release(self) -> None:
        """Invalidate the attempt and clear UI state now, then stop the server work."""
        with self._lock:
            self.generation += 1
            self.busy = False
            self.hint = ""
            source = self.source
            self.source = None
        if source is not None:
            source.cancel()


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
        self.orca_login = OrcaLoginSession()
        self.orca_models: dict[str, list[str]] = {"chat": [], "chat+image": []}
        self.orca_catalog_state = {"source": "", "degraded": False, "error": ""}
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 760, 648),
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
        view = A.NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 648))
        view.setMaterial_(getattr(
            A, "NSVisualEffectMaterialSidebar",
            getattr(A, "NSVisualEffectMaterialLight", 1)))
        view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        view.setState_(A.NSVisualEffectStateActive)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.window.setContentView_(view)

        title = self.label(view, "模型设置", 24, 598, 710, 28, 22)
        title.setFont_(A.NSFont.boldSystemFontOfSize_(22))
        title.setTextColor_(PALETTE["text"])
        self.label(view, "编辑文件：" + str(self.path).replace(str(Path.home()), "~"),
                   24, 570, 710, 20, 11, PALETTE["muted"])

        restart_box = ui_style.make_surface(
            10, PALETTE["amber"].colorWithAlphaComponent_(0.10),
            PALETTE["amber"].colorWithAlphaComponent_(0.18))
        restart_box.setFrame_(NSMakeRect(24, 530, 710, 34))
        view.addSubview_(restart_box)
        restart_notice = self.label(view, "保存后请退出应用并重启",
                                    38, 536, 680, 20, 13, PALETTE["amber"])
        restart_notice.setFont_(A.NSFont.boldSystemFontOfSize_(13))

        tab_surface = ui_style.make_surface(14, PALETTE["surface"], PALETTE["edge"])
        tab_surface.setFrame_(NSMakeRect(16, 176, 728, 342))
        view.addSubview_(tab_surface)
        self.tabs = A.NSTabView.alloc().initWithFrame_(NSMakeRect(24, 184, 712, 326))
        if hasattr(self.tabs, "setDrawsBackground_"):
            self.tabs.setDrawsBackground_(False)
        self.tabs.setDelegate_(self)
        titles = ("判断 · Jev", "生成 · OpenAI 兼容", "生成 · Anthropic 兼容",
                  "生成 · OrcaRouter")
        for index, (prefix, title) in enumerate(zip(config.PREFIXES, titles)):
            item = A.NSTabViewItem.alloc().initWithIdentifier_(prefix)
            item.setLabel_(title)
            panel = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 690, 300))
            # OrcaRouter's page carries two authentication entrances as well as the three
            # fields, so it uses a compact status strip and its own row geometry.
            orca = prefix == "ORCAROUTER"
            strip_y = 230 if orca else 212
            strip_h = 58 if orca else 76
            badge_y = 268 if orca else 254
            source_y = 232 if orca else 220
            field_ys = {"API_KEY": 192, "BASE_URL": 154, "MODEL": 116} if orca else \
                {"API_KEY": 166, "BASE_URL": 120, "MODEL": 74}
            summary, source = self.current_source(prefix)
            source_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
            source_surface.setFrame_(NSMakeRect(12, strip_y, 666, strip_h))
            panel.addSubview_(source_surface)
            badge = self.label(panel, summary, 26, badge_y, 638, 20, 14, PALETTE["green"])
            badge.setFont_(A.NSFont.boldSystemFontOfSize_(14))
            self.label(panel, source, 26, source_y, 638, 34, 11, PALETTE["muted"])
            fields = {}
            for name in ("API_KEY", "BASE_URL", "MODEL"):
                label = {"API_KEY": "密钥", "BASE_URL": "服务地址", "MODEL": "模型"}[name]
                y = field_ys[name]
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
                    field.setPlaceholderString_(
                        "从在线目录选择（不支持手填）" if orca
                        else "获取模型列表后选择，或手动填写模型名称")
                panel.addSubview_(field)
                fields[name] = field
                self.initial[f"{prefix}_{name}"] = value
                self.controls.append(field)
            self.fields[prefix] = fields
            hint = ("Jev 地址带不带 /v1 都行，网关动作不同时可填完整动作路径；列表接口不可用时可手填模型。" if prefix == "TYPESAFE"
                    else "可手填模型。Ollama 地址通常含 /v1，密钥可填 ollama。" if prefix == "OPENAI"
                    else "使用 Anthropic 消息接口，支持自定义兼容服务地址。" if prefix == "ANTHROPIC"
                    else "模型只能从在线目录选择；密钥与账号登录二选一即可，随时可切换。")
            hint_label = self.label(panel, hint, 26, 43, 638, 20, 11, PALETTE["muted"])
            if orca:
                # Two explicit entrances, side by side, above the shared model dropdown.
                auth_surface = ui_style.make_surface(10, PALETTE["row"], PALETTE["edge"])
                auth_surface.setFrame_(NSMakeRect(12, 62, 666, 44))
                panel.addSubview_(auth_surface)
                self.label(panel, "接入方式", 26, 76, 68, 20, 11, PALETTE["text"])
                self.orca_login_button = self.button(panel, "Connect with OrcaRouter", "orcaLogin:",
                                                     104, 68, 206)
                self.orca_login_button.setToolTip_(
                    "在浏览器中用 OrcaRouter 账号授权，授权后自动写入密钥")
                self.orca_login_button.setAccessibilityLabel_("Connect with OrcaRouter")
                self.orca_cancel_button = self.button(panel, "取消登录", "orcaCancel:", 318, 68, 96)
                self.orca_cancel_button.setEnabled_(False)
                self.orca_cancel_button.setAccessibilityLabel_("取消 OrcaRouter 登录")
                self.controls.extend((self.orca_login_button, self.orca_cancel_button))
                self.orca_status = self.label(panel, "", 424, 78, 250, 20, 11, PALETTE["muted"])
                hint_label.setStringValue_(
                    "两种接入方式二选一：在上方粘贴已有 sk-orca-… 密钥，或点右侧按钮用账号登录；"
                    "模型只能从在线目录选择。")
                hint_label.setFrame_(NSMakeRect(26, 40, 638, 18))
            for text, action, x in (("获取模型列表", "fetchModels:", 372), ("测试连接", "testConnection:", 524)):
                button = self.button(panel, text, action, x, 4, 140)
                button.setTag_(index)
                self.controls.append(button)
            item.setView_(panel)
            self.tabs.addTabViewItem_(item)
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

    # ---------------------------------------------------------------- OrcaRouter

    @objc.python_method
    def orca_form(self):
        return self.values("ORCAROUTER")

    def orcaLogin_(self, sender):
        """Entrance 2 — OAuth 2.0 + PKCE, Flow A (loopback), in a background thread."""
        if self.busy or self.orca_login.busy:
            return
        self.window.makeFirstResponder_(None)
        try:
            base = config.validate_orcarouter_endpoint(self.orca_form()["BASE_URL"])
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        generation = self.orca_login.begin()
        self.orca_login_button.setEnabled_(False)
        self.orca_cancel_button.setEnabled_(True)
        self.orca_status.setStringValue_("正在打开浏览器…")
        self.orca_status.setTextColor_(PALETTE["muted"])

        def work():
            source = orcarouter.PkceSource(base)
            with self.orca_login._lock:
                if generation != self.orca_login.generation:
                    return
                self.orca_login.source = source
            try:
                url = source.start()
                if not self.orca_login.current(generation):
                    return
                self.orca_login.set_hint(generation, url)
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "orcaAuthorizeReady:", {"generation": generation, "url": url}, False)
                cred = source.finish()
            except orcarouter.LoginCancelled:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "orcaLoginFinished:", {"generation": generation, "cancelled": True}, False)
                return
            except orcarouter.OrcaRouterError as e:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "orcaLoginFinished:", {"generation": generation, "error": str(e)}, False)
                return
            except Exception as e:                       # never surface a raw traceback
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "orcaLoginFinished:",
                    {"generation": generation, "error": f"{type(e).__name__}"}, False)
                return
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "orcaLoginFinished:", {"generation": generation, "credential": cred}, False)

        threading.Thread(target=work, daemon=True).start()

    def orcaAuthorizeReady_(self, info):
        """The browser is about to open; also offer the URL for a browser that did not."""
        generation = info["generation"]
        if not self.orca_login.current(generation):
            return
        url = info["url"]
        self.orca_status.setStringValue_("等待浏览器授权…（取消登录可中止）")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        # The URL is not a secret — it carries the challenge, never the verifier — but it is
        # long, so it goes to the clipboard and the status line says where to find it.
        try:
            A.NSPasteboard.generalPasteboard().clearContents()
            A.NSPasteboard.generalPasteboard().setString_forType_(url, A.NSPasteboardTypeString)
            self.set_status("已在浏览器打开授权页；未自动打开时授权链接已复制到剪贴板。")
        except Exception:
            self.set_status("已在浏览器打开授权页；未自动打开时请重试。")

    def orcaLoginFinished_(self, info):
        generation = info["generation"]
        if not self.orca_login.current(generation):
            return                       # a newer attempt (or a cancel) already owns the UI
        self.orca_login.settle(generation)
        self.orca_login_button.setEnabled_(True)
        self.orca_cancel_button.setEnabled_(False)
        if info.get("cancelled"):
            self.orca_status.setStringValue_("已取消登录。")
            self.orca_status.setTextColor_(PALETTE["muted"])
            return
        if info.get("error"):
            self.orca_status.setStringValue_("登录未完成。")
            self.orca_status.setTextColor_(PALETTE["red"])
            self.set_status(info["error"], "error")
            return
        credential = info["credential"]
        try:
            self.persist_orca_credential(credential)
        except (ValueError, OSError) as e:
            self.orca_status.setStringValue_("登录成功但写入失败。")
            self.orca_status.setTextColor_(PALETTE["red"])
            self.set_status(f"写入配置失败：{str(e)[:80]}", "error")
            return
        orcarouter.clear_reauth()
        self.orca_status.setStringValue_("已登录，密钥已保存。")
        self.orca_status.setTextColor_(PALETTE["green"])
        self.set_status("已通过账号登录取得密钥并写入配置文件。重启应用后生效；"
                        "如需撤销，可在 OrcaRouter 控制台的「已授权应用」中一键撤销。", "success")
        self.refresh_orca_catalog()

    def orcaCancel_(self, sender):
        """Explicit cancel: clear UI state first, then stop the listener and the wait."""
        if not self.orca_login.busy:
            return
        self.orca_login.release()
        self.orca_login_button.setEnabled_(True)
        self.orca_cancel_button.setEnabled_(False)
        self.orca_status.setStringValue_("已取消登录。")
        self.orca_status.setTextColor_(PALETTE["muted"])
        self.set_status("已取消 OrcaRouter 登录。")

    @objc.python_method
    def persist_orca_credential(self, credential):
        """Write the key through the project's existing guarded settings writer.

        OrcaRouter's consent screen can be re-approved by the same person at any time, and
        there is no way for this app to tell a pasted key from a signed-in one after the
        fact — both are the same durable `sk-orca-…`. So the key replaces whatever is in the
        file, and the account id is written alongside as a label. The old secret is not
        deleted before this write succeeds; a failed write leaves the previous key intact.
        """
        changes = {f"{orcarouter.PREFIX}_API_KEY": credential.key,
                   f"{orcarouter.PREFIX}_BASE_URL": config.validate_orcarouter_endpoint(
                       self.orca_form()["BASE_URL"])}
        if credential.account:
            changes["ORCAROUTER_ACCOUNT_ID"] = credential.account
        self.original = config.write_settings(self.path, self.original, changes)
        self.file_values.update(changes)
        for name, value in (("API_KEY", credential.key),
                            ("BASE_URL", changes[f"{orcarouter.PREFIX}_BASE_URL"])):
            self.fields["ORCAROUTER"][name].setStringValue_(value)
            self.initial[f"ORCAROUTER_{name}"] = value

    @objc.python_method
    def refresh_orca_catalog(self, capability="chat", require_input=""):
        """Ask the live catalog for one capability; never a free-text model field."""
        values = self.orca_form()
        base = values["BASE_URL"]
        key = values["API_KEY"]
        if key and not orcarouter.looks_like_key(key):
            self.set_status("密钥格式不像 sk-orca-… 开头的 OrcaRouter 密钥，请检查。", "error")
            return
        catalog = config.list_orcarouter_models(base, key, capability, require_input)
        self.apply_orca_catalog(catalog, capability, require_input)

    @objc.python_method
    def apply_orca_catalog(self, catalog, capability="chat", require_input=""):
        """Bind the filtered catalog to the dropdown and report where it came from."""
        self.orca_catalog_state = {"source": catalog.source, "degraded": catalog.degraded,
                                   "error": catalog.error}
        models = orcarouter.selector_options(catalog)
        self.orca_models[f"{capability}+{require_input}".rstrip("+")] = models
        combo = self.fields["ORCAROUTER"]["MODEL"]
        value, invalidated = orcarouter.selection_after_catalog(combo.stringValue(), models)
        self.set_models(combo, models)
        combo.setStringValue_(value)
        if invalidated:
            # A model that is no longer offered (or no longer fits the required capability)
            # must not stay selected: clear it and say why.
            self.set_status("原模型不在当前目录中，已清空，请重新选择。", "error")
        elif catalog.degraded:
            self.set_status(f"在线目录不可用（{catalog.error}），"
                            f"当前显示已验证的备用清单 {len(models)} 个。", "error")
        else:
            self.set_status(f"已获取 {len(models)} 个可用模型（能力：{capability}"
                            + (f"，输入：{require_input}" if require_input else "") + "）。",
                            "success")

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
        elif prefix == "ORCAROUTER":
            cred = orcarouter.resolve_credential()
            if cred.present:
                entrance = ("账号登录（OAuth 2.0 + PKCE）" if cred.source == "pkce"
                            else "手填密钥")
                summary = f"本次启动：正在使用 OrcaRouter 密钥（{entrance}）"
                if orcarouter.needs_reauth(cred):
                    summary = "本次启动：OrcaRouter 密钥已被服务端拒绝，需重新登录"
                source = f"密钥：{orcarouter.mask(cred.key)}\n推理地址：{orcarouter.api_base()}"
            else:
                summary = "本次启动：未配置 OrcaRouter 密钥"
                source = f"推理地址：{orcarouter.api_base()}"
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
        for prefix, fields in self.fields.items():
            if field in (fields["API_KEY"], fields["BASE_URL"]):
                combo = fields["MODEL"]
                self.set_models(combo, [])
                if prefix == "ORCAROUTER" and field is fields["API_KEY"]:
                    self.set_status("密钥已修改，请重新获取模型列表；保存后重启生效。")
                    return
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
                        if prefix == "ORCAROUTER":
                            raise ValueError("填写密钥后，请同时填写地址并选择模型。")
                        raise ValueError("填写密钥后，请同时填写该服务的地址和模型。")
            for key, value in changes.items():
                if key.endswith("_BASE_URL") and value:
                    if key.startswith("ORCAROUTER_"):
                        config.validate_orcarouter_endpoint(value)
                    else:
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
            if prefix == "ORCAROUTER":
                config.validate_orcarouter_endpoint(values["BASE_URL"])
                if not values["API_KEY"]:
                    raise ValueError("请填写密钥，或点击「Connect with OrcaRouter」用账号登录。")
                if not listing and not values["MODEL"].strip():
                    raise ValueError("请先从在线目录选择模型，再测试连接。")
                if not listing:
                    config.test_connection(prefix, values["BASE_URL"], values["API_KEY"],
                                           values["MODEL"])
                    self.set_status("连接成功：OrcaRouter 返回了有效结果。"
                                    "配置尚需保存并重启生效。", "success")
                    return
                # The capability-filtered live catalog is the only source for the
                # OrcaRouter dropdown. It goes out on a worker thread like every other
                # provider request: a catalog fetch must not freeze the window.
                self.busy = True
                for control in self.controls:
                    control.setEnabled_(False)
                self.set_status("正在获取模型列表…")

                def list_orca():
                    try:
                        catalog = config.list_orcarouter_models(
                            values["BASE_URL"], values["API_KEY"])
                        result = {"index": index, "listing": True, "orca": catalog}
                    except Exception as e:                 # noqa: BLE001
                        result = {"index": index, "listing": True,
                                  "error": config.error_message(e)}
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "requestFinished:", result, False)

                threading.Thread(target=list_orca, daemon=True).start()
                return
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
        except Exception as e:
            self.set_status(config.error_message(e), "error")
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
        if "orca" in result:
            self.apply_orca_catalog(result["orca"])
            return
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
        # Leaving the window ends any authorization attempt: clear busy/hint synchronously
        # and stop the loopback listener, rather than leaving the socket and the panel
        # state alive for a window nobody is looking at.
        self.release_orca_login()
        if self.changed():
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_("放弃尚未保存的配置？")
            alert.addButtonWithTitle_("继续编辑")
            alert.addButtonWithTitle_("放弃修改")
            return alert.runModal() == A.NSAlertSecondButtonReturn
        return True

    def windowWillClose_(self, notification):
        self.release_orca_login()

    def tabView_didSelectTabViewItem_(self, tab_view, item):
        """Switching provider tabs abandons an in-flight OrcaRouter login.

        This fires while the tab items are being added, before the OrcaRouter page has
        built its buttons, so every widget here is looked up defensively.
        """
        if item.identifier() != "ORCAROUTER":
            self.release_orca_login()

    @objc.python_method
    def release_orca_login(self):
        """One teardown path for cancel, tab switch, window close and app termination."""
        self.orca_login.release()
        for name, enabled, text in (("orca_login_button", True, ""),
                                    ("orca_cancel_button", False, ""),
                                    ("orca_status", None, "")):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            if enabled is not None:
                widget.setEnabled_(enabled)
            elif text:
                widget.setStringValue_(text)
        status = getattr(self, "orca_status", None)
        if status is not None:
            status.setStringValue_("")

    def applicationWillTerminate_(self, notification):
        self.release_orca_login()


if __name__ == "__main__":
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    userconfig.load()
    controller = SettingsController.alloc().init().build()
    # The app delegate so a quit runs the same login teardown as closing the window.
    app.setDelegate_(controller)
    controller.show()
    app.run()
