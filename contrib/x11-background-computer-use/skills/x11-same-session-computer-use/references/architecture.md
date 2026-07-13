# Architecture and guarantees

The MCP broker attaches only to the inherited local Unix `DISPLAY` and Xauthority of the Codex process. It requires logind to positively identify an active, non-remote X11 login. Window ownership comes from the XRes server extension rather than spoofable `_NET_WM_PID` metadata. It rejects windows whose authenticated process UID, display, or session differs.

`wmctrl -lpGx` supplies the EWMH client list. XIDs are exact identifiers but live only as long as the X client window. The broker reports both the advisory EWMH PID and the authenticated XRes PID. PID, title, and WM_CLASS are returned as hints for the separate AT-SPI Computer Use plugin.

Exact capture uses `XCompositeNameWindowPixmap`, `XGetImage`, and libpng in a small source-built helper. It works for mapped windows while a compositing manager owns `_NET_WM_CM_Sn`, including obscured windows. Capture on another desktop depends on whether that window manager keeps the client mapped and whether its compositor retains the pixmap. The helper rejects minimized/unmapped windows and non-composited desktops instead of returning a misleading root-screen crop. Builds are same-user, cached by source hash, file-locked, and require no root daemon.

`send_window_shortcut` uses window-targeted XSendEvent through xdotool. This preserves focus but modern GTK, Qt, Chromium, Electron, and applications that inspect the synthetic bit may ignore it. The broker therefore reports delivery as unconfirmed.

Reliable input uses XTEST under an explicit lease. Before focusing the target, the broker journals the verified logind session, X socket inode, authenticated WM process lifetime, window identities, active window, desktop, pointer, and target minimized state. Recovery refuses all mutation if those bindings changed. Pointer calls restore their starting position. Drags journal the pressed button before `mousedown`, release it in `finally`, and leave enough state for recovery after a crash. Final restoration is best effort and the journal remains when any restoration step fails.

MCP request cancellation or stdin closure does not interrupt a mutation already running in a worker. The broker waits for all workers before exit so cleanup and journal updates finish.

This backend does not promise non-interfering background pointer or reliable keyboard delivery. Those capabilities are impossible to provide generically on stock X11 without application cooperation or a separate input seat.
