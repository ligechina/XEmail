(function () {
  function _setVisible(el, visible) {
    if (!el) return;
    el.style.display = visible ? "" : "none";
  }

  function paintModePill(pill, mode, t) {
    if (!pill) return;
    const tr = typeof t === "function" ? t : (s) => s;
    const isProd = mode === "prod";
    pill.textContent = isProd ? tr("系统模式：使用态") : tr("系统模式：开发态");
    pill.classList.toggle("mode-prod", isProd);
  }

  function paintDesktopPills(opts) {
    const tr = typeof opts.t === "function" ? opts.t : (s) => s;
    const trayPill = opts.trayPill;
    const autoPill = opts.autostartPill;
    const hintEl = opts.hintEl;
    const data = opts.data || {};
    const isAdmin = !!opts.isAdmin;

    _setVisible(trayPill, isAdmin);
    _setVisible(autoPill, isAdmin);
    if (!isAdmin) return;

    if (trayPill) {
      trayPill.textContent = data.enable_tray ? tr("托盘：已启用") : tr("托盘：已关闭");
      trayPill.classList.toggle("on", !!data.enable_tray);
    }

    if (!autoPill) return;
    if (!data.autostart_supported) {
      autoPill.textContent = tr("开机启动：当前系统不支持");
      autoPill.classList.remove("on");
      if (hintEl) hintEl.textContent = tr("开机启动开关仅支持 macOS（LaunchAgent）。");
      return;
    }

    autoPill.textContent = data.autostart_enabled ? tr("开机启动：已启用") : tr("开机启动：已关闭");
    autoPill.classList.toggle("on", !!data.autostart_enabled);
    if (hintEl) {
      hintEl.textContent = data.autostart_enabled
        ? tr("当前已启用开机启动。")
        : tr("当前未启用开机启动。");
    }
  }

  window.RuntimeStatus = {
    paintModePill,
    paintDesktopPills,
  };
})();
