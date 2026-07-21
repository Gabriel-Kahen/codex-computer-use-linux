import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {LeaseProtocol} from './lease_protocol.js';
import {
    beginClaimedLeaseAsync,
    renewLeaseAsync,
} from './lease_dbus.js';
import {
    actAndCaptureTransaction,
    activateLeaseTransaction,
    assertInputSafe,
    injectKeyTransaction,
    injectPointerTransaction,
    restoreLeaseTransaction,
} from './shell_transactions.js';

const BUS_NAME = 'org.gnome.Shell.Extensions.BackgroundComputerUse';
const OBJECT_PATH = '/org/gnome/Shell/Extensions/BackgroundComputerUse';
const PROTOCOL_VERSION = 4;
const MAX_CAPTURE_BYTES = 5 * 1024 * 1024;
const MAX_CAPTURE_PIXELS = 7680 * 4320;
const XML = `<node>
  <interface name="org.gnome.Shell.Extensions.BackgroundComputerUse">
    <method name="Status"><arg type="s" direction="out"/></method>
    <method name="ListWindows"><arg type="s" direction="out"/></method>
    <method name="BeginLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="BeginClaimedLease"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="ActivateLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RenewLease"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RestoreLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RecoverLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="InjectPointer"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="InjectKeys"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="CaptureWindow"><arg type="s" direction="in"/><arg type="ay" direction="out"/><arg type="s" direction="out"/></method>
    <method name="ActAndCapture"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="ay" direction="out"/><arg type="s" direction="out"/></method>
  </interface>
</node>`;

Gio._promisify(Shell.Screenshot, 'composite_to_stream');

function json(value) {
    return JSON.stringify(value);
}

export default class BackgroundComputerUseExtension extends Extension {
    enable() {
        this._tracker = Shell.WindowTracker.get_default();
        this._seat = Clutter.get_default_backend().get_default_seat();
        this._pointer = this._seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._keyboard = this._seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._protocol = new LeaseProtocol();
        this._shellInstance = GLib.uuid_string_random();
        this._leaseExpiryId = null;
        this._captureActive = false;
        this._object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._object.export(Gio.DBus.session, OBJECT_PATH);
        this._owner = Gio.bus_own_name_on_connection(
            Gio.DBus.session,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null,
        );
    }

    disable() {
        if (this._leaseExpiryId)
            GLib.source_remove(this._leaseExpiryId);
        if (this._owner)
            Gio.bus_unown_name(this._owner);
        this._object?.unexport();
        this._owner = null;
        this._object = null;
        this._pointer = null;
        this._keyboard = null;
        this._seat = null;
        this._tracker = null;
        this._protocol?.clear();
        this._protocol = null;
        this._shellInstance = null;
        this._leaseExpiryId = null;
        this._captureActive = false;
    }

    _windows() {
        return global.get_window_actors()
            .map(actor => actor.meta_window)
            .filter(window => window && !window.is_override_redirect());
    }

    _id(window) {
        return String(window.get_stable_sequence());
    }

    _find(id) {
        const window = this._windows().find(candidate => this._id(candidate) === id);
        if (!window)
            throw new Error(`window ${id} no longer exists`);
        return window;
    }

    _window(window) {
        const frame = window.get_frame_rect();
        const workspace = window.get_workspace();
        const app = this._tracker.get_window_app(window);
        return {
            id: this._id(window),
            title: window.get_title() ?? '',
            wm_class: window.get_wm_class() ?? '',
            app_id: app?.get_id() ?? window.get_gtk_application_id() ?? '',
            pid: window.get_pid(),
            workspace: workspace?.index() ?? null,
            monitor: window.get_monitor(),
            focused: window.has_focus(),
            minimized: window.minimized,
            fullscreen: window.is_fullscreen(),
            client_type: window.get_client_type() === Meta.WindowClientType.WAYLAND ? 'wayland' : 'x11',
            frame: {x: frame.x, y: frame.y, width: frame.width, height: frame.height},
        };
    }

    _state() {
        const focused = global.display.get_focus_window();
        const [x, y] = global.get_pointer();
        return {
            focused_window: focused ? this._id(focused) : null,
            workspace: global.workspace_manager.get_active_workspace_index(),
            pointer: {x, y},
            overview_visible: Main.overview.visible,
        };
    }

    _assertInputSafe() {
        assertInputSafe({
            locked: Main.sessionMode.isLocked,
            overviewVisible: Main.overview.visible,
            modalCount: Main.modalCount,
            grabbed: global.display.is_grabbed(),
        });
    }

    _requireLease(capability, sender, phase = null) {
        return this._protocol.require(capability, sender, phase);
    }

    _reply(invocation, callback) {
        try {
            invocation.return_value(new GLib.Variant('(s)', [json(callback())]));
        } catch (error) {
            invocation.return_dbus_error(`${BUS_NAME}.Error`, String(error.message ?? error));
        }
    }

    _nameHasOwner(name) {
        const result = Gio.DBus.session.call_sync(
            'org.freedesktop.DBus',
            '/org/freedesktop/DBus',
            'org.freedesktop.DBus',
            'NameHasOwner',
            new GLib.Variant('(s)', [name]),
            new GLib.VariantType('(b)'),
            Gio.DBusCallFlags.NONE,
            -1,
            null,
        );
        return result.deepUnpack()[0];
    }

    _cancelPendingExpiry() {
        if (this._leaseExpiryId)
            GLib.source_remove(this._leaseExpiryId);
        this._leaseExpiryId = null;
    }

    _clearLease() {
        this._cancelPendingExpiry();
        this._protocol.clear();
    }

    Status() {
        return json({
            protocol_version: PROTOCOL_VERSION,
            capabilities: ['claimed_focus_leases', 'window_actor_capture', 'act_and_capture'],
            shell_version: Config.PACKAGE_VERSION,
            locked: Main.sessionMode.isLocked,
            overview_visible: Main.overview.visible,
            modal_count: Main.modalCount,
            grab_active: global.display.is_grabbed(),
            lease_phase: this._protocol.lease?.phase ?? null,
            claimed_lease_recoverable: this._protocol.lease?.recoveryDeadlineUsec != null,
            shell_instance: this._shellInstance,
        });
    }

    ListWindows() {
        return json(this._windows().map(window => this._window(window)));
    }

    CaptureWindowAsync([id], invocation) {
        this._captureWindow(id)
            .then(({bytes, metadata}) => {
                invocation.return_value(new GLib.Variant('(ays)', [bytes, json(metadata)]));
            })
            .catch(error => {
                invocation.return_dbus_error(
                    `${BUS_NAME}.Error`, String(error.message ?? error));
            });
    }

    async _captureWindow(id) {
        if (Main.sessionMode.isLocked)
            throw new Error('cannot capture a window while the GNOME session is locked');
        if (this._captureActive)
            throw new Error('another window capture is already in progress');

        const window = this._find(id);
        const actor = window.get_compositor_private();
        if (!actor || actor.is_destroyed())
            throw new Error(`window ${id} has no live compositor actor`);

        this._captureActive = true;
        try {
            const content = actor.paint_to_content(null);
            if (!content)
                throw new Error(`window ${id} has no capturable compositor content`);
            const texture = content.get_texture();
            if (!texture)
                throw new Error(`window ${id} has no capturable compositor texture`);
            const width = texture.get_width();
            const height = texture.get_height();
            if (width <= 0 || height <= 0 || width * height > MAX_CAPTURE_PIXELS)
                throw new Error(`window ${id} capture dimensions are outside the supported bounds`);

            const stream = Gio.MemoryOutputStream.new_resizable();
            await Shell.Screenshot.composite_to_stream(
                texture,
                0, 0, -1, -1,
                1,
                null, 0, 0, 1,
                stream,
            );
            stream.close(null);
            const bytes = stream.steal_as_bytes().get_data();
            if (bytes.length > MAX_CAPTURE_BYTES)
                throw new Error(`captured PNG exceeds the ${MAX_CAPTURE_BYTES}-byte transport limit`);

            const current = this._find(id);
            if (current.get_compositor_private() !== actor || actor.is_destroyed())
                throw new Error(`window ${id} changed while it was being captured`);
            return {
                bytes,
                metadata: {
                    window: this._window(current),
                    screenshot_pixels: {width, height},
                    shell_instance: this._shellInstance,
                    source: 'meta-window-actor',
                    potentially_stale: Boolean(current.minimized),
                },
            };
        } finally {
            this._captureActive = false;
        }
    }

    _armWindowFrame(window, timeoutMs = 180) {
        const actor = window.get_compositor_private();
        if (!actor || actor.is_destroyed()) {
            return {
                promise: Promise.resolve({reason: 'actor-unavailable'}),
                cancel: () => {},
            };
        }

        let cancel = null;
        const promise = new Promise(resolve => {
            let damageId = 0;
            let destroyId = 0;
            let afterPaintId = 0;
            let timeoutId = 0;
            const finish = reason => {
                if (damageId)
                    actor.disconnect(damageId);
                if (destroyId)
                    actor.disconnect(destroyId);
                if (afterPaintId)
                    global.stage.disconnect(afterPaintId);
                if (timeoutId)
                    GLib.source_remove(timeoutId);
                damageId = 0;
                destroyId = 0;
                afterPaintId = 0;
                timeoutId = 0;
                resolve({reason});
            };
            cancel = () => finish('cancelled');
            damageId = actor.connect('damaged', () => {
                if (afterPaintId)
                    return;
                afterPaintId = global.stage.connect('after-paint', () => {
                    finish('damaged-and-painted');
                });
                global.stage.queue_redraw();
            });
            destroyId = actor.connect('destroy', () => finish('actor-destroyed'));
            timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
                timeoutId = 0;
                finish('timeout');
                return GLib.SOURCE_REMOVE;
            });
        });
        return {promise, cancel: () => cancel()};
    }

    ActAndCaptureAsync([capability, serialized], invocation) {
        this._actAndCapture(capability, serialized, invocation.get_sender())
            .then(({bytes, metadata}) => {
                invocation.return_value(new GLib.Variant('(ays)', [bytes, json(metadata)]));
            })
            .catch(error => {
                invocation.return_dbus_error(
                    `${BUS_NAME}.Error`, String(error.message ?? error));
            });
    }

    async _actAndCapture(capability, serialized, sender) {
        const lease = this._requireLease(capability, sender, 'active');
        const request = JSON.parse(serialized);
        const {captured, transaction} = await actAndCaptureTransaction(request, {
            armWindowFrame: () => this._armWindowFrame(this._find(lease.target)),
            injectPointer: action => this._injectPointer(capability, json(action), sender),
            injectKeys: action => this._injectKeys(capability, json(action), sender),
            capture: () => this._captureWindow(lease.target),
            restore: () => this._restoreLease(lease),
            monotonicTimeUsec: () => GLib.get_monotonic_time(),
        });
        captured.metadata.transaction = transaction;
        return captured;
    }

    BeginLeaseAsync([id], invocation) {
        this._reply(invocation, () => this._beginLease(id, invocation.get_sender()));
    }

    _beginLease(id, owner) {
        return this._beginLeaseWithRecovery(id, owner, null);
    }

    BeginClaimedLeaseAsync([id, recoverySeconds], invocation) {
        beginClaimedLeaseAsync([id, recoverySeconds], invocation, {
            beginLease: (target, owner, recoveryDeadlineUsec) =>
                this._beginLeaseWithRecovery(target, owner, recoveryDeadlineUsec),
            encodeReply: value => new GLib.Variant('(s)', [json(value)]),
            errorName: `${BUS_NAME}.Error`,
            monotonicTimeUsec: () => GLib.get_monotonic_time(),
        });
    }

    _beginLeaseWithRecovery(id, owner, recoveryDeadlineUsec) {
        this._assertInputSafe();
        const window = this._find(id);
        const capability = `${GLib.uuid_string_random()}${GLib.uuid_string_random()}`;
        const lease = this._protocol.begin({
            capability,
            owner,
            target: id,
            targetMinimized: Boolean(window.minimized),
            original: this._state(),
            recoveryDeadlineUsec,
        });
        this._leaseExpiryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            this._leaseExpiryId = null;
            this._protocol.expirePending();
            return GLib.SOURCE_REMOVE;
        });
        return {
            capability,
            phase: 'pending',
            target: this._window(window),
            original: lease.original,
            shell_instance: this._shellInstance,
        };
    }

    ActivateLeaseAsync([capability], invocation) {
        this._reply(invocation, () => this._activateLease(capability, invocation.get_sender()));
    }

    RenewLeaseAsync([capability, recoverySeconds], invocation) {
        renewLeaseAsync([capability, recoverySeconds], invocation, {
            encodeReply: value => new GLib.Variant('(s)', [json(value)]),
            errorName: `${BUS_NAME}.Error`,
            monotonicTimeUsec: () => GLib.get_monotonic_time(),
            renewLease: (token, owner, deadline) =>
                this._protocol.renew(token, owner, deadline),
        });
    }

    _activateLease(capability, sender) {
        const lease = this._requireLease(capability, sender, 'pending');
        this._assertInputSafe();
        const time = global.get_current_time();
        const state = activateLeaseTransaction(lease, {
            findWindow: id => this._windows().find(window => this._id(window) === id) ?? null,
            workspaceForWindow: window => window.get_workspace(),
            markLeaseActive: () => this._protocol.activate(capability, sender),
            activateWorkspace: workspace => workspace.activate(time),
            unminimizeWindow: window => window.unminimize(),
            focusWindow: window => window.activate(time),
            state: () => this._state(),
        });
        this._cancelPendingExpiry();
        return {focused: lease.target, state};
    }

    RestoreLeaseAsync([capability], invocation) {
        this._reply(invocation, () => {
            const lease = this._requireLease(capability, invocation.get_sender());
            return this._restoreLease(lease);
        });
    }

    RecoverLeaseAsync([capability], invocation) {
        this._reply(invocation, () => {
            const sender = invocation.get_sender();
            const currentOwner = this._protocol.lease?.owner;
            const ownerPresent = currentOwner && sender !== currentOwner
                ? this._nameHasOwner(currentOwner)
                : false;
            return this._restoreLease(this._protocol.recover(
                capability, sender, ownerPresent, GLib.get_monotonic_time()));
        });
    }

    _restoreLease(lease) {
        if (Main.sessionMode.isLocked)
            throw new Error('cannot restore a focus lease while the GNOME session is locked');
        if (lease.phase === 'pending') {
            this._clearLease();
            return {
                restored: true,
                recovery_complete: true,
                errors: [],
                missing_windows: [],
                state: this._state(),
            };
        }
        const time = global.get_current_time();
        const result = restoreLeaseTransaction(lease, {
            findWindow: id => this._windows().find(window => this._id(window) === id) ?? null,
            workspaceForWindow: window => window.get_workspace(),
            workspaceByIndex: index => global.workspace_manager.get_workspace_by_index(index),
            activateWorkspace: workspace => workspace.activate(time),
            focusWindow: window => window.activate(time),
            minimizeWindow: window => window.minimize(),
            isMinimized: window => window.minimized,
            movePointer: point => this._pointer.notify_absolute_motion(
                GLib.get_monotonic_time(), point.x, point.y),
            state: () => this._state(),
        });
        this._protocol.finishRestore(result.recovery_complete);
        return result;
    }

    InjectPointerAsync([capability, serialized], invocation) {
        this._reply(invocation, () => this._injectPointer(capability, serialized, invocation.get_sender()));
    }

    _injectPointer(capability, serialized, sender) {
        const lease = this._requireLease(capability, sender, 'active');
        this._assertInputSafe();
        const request = JSON.parse(serialized);
        const window = this._find(lease.target);
        if (!window.has_focus())
            throw new Error('pointer injection requires the leased window to remain focused');
        const frame = window.get_frame_rect();
        const now = () => GLib.get_monotonic_time();
        const buttons = {left: 0x110, right: 0x111, middle: 0x112};
        injectPointerTransaction(request, frame, {
            pointer: () => {
                const [x, y] = global.get_pointer();
                return {x, y};
            },
            movePointer: point => this._pointer.notify_absolute_motion(now(), point.x, point.y),
            pressButton: button => this._pointer.notify_button(
                now(), buttons[button], Clutter.ButtonState.PRESSED),
            releaseButton: button => this._pointer.notify_button(
                now(), buttons[button], Clutter.ButtonState.RELEASED),
            scroll: direction => this._pointer.notify_discrete_scroll(
                now(), direction === 'down' ? Clutter.ScrollDirection.DOWN : Clutter.ScrollDirection.UP,
                Clutter.ScrollSource.WHEEL),
        });
        return {action: request.action, window: lease.target, pointer_restored: true};
    }

    InjectKeysAsync([capability, serialized], invocation) {
        this._reply(invocation, () => this._injectKeys(capability, serialized, invocation.get_sender()));
    }

    _injectKeys(capability, serialized, sender) {
        const lease = this._requireLease(capability, sender, 'active');
        this._assertInputSafe();
        const request = JSON.parse(serialized);
        const window = this._find(lease.target);
        if (!window.has_focus())
            throw new Error('keyboard injection requires the leased window to remain focused');
        const modifierNames = {CTRL: 'Control_L', SHIFT: 'Shift_L', ALT: 'Alt_L', SUPER: 'Super_L'};
        const keyval = name => {
            if (name.length === 1)
                return Clutter.unicode_to_keysym(name.codePointAt(0));
            const aliases = {
                ENTER: 'Return', RETURN: 'Return', ESC: 'Escape', SPACE: 'space', TAB: 'Tab',
                BACKSPACE: 'BackSpace', DELETE: 'Delete', UP: 'Up', DOWN: 'Down', LEFT: 'Left',
                RIGHT: 'Right', PAGE_UP: 'Page_Up', PAGE_DOWN: 'Page_Down', HOME: 'Home', END: 'End',
            };
            const value = Clutter[`KEY_${aliases[name] ?? name}`];
            if (value === undefined)
                throw new Error(`unknown Clutter key name ${name}`);
            return value;
        };
        const now = () => GLib.get_monotonic_time();
        injectKeyTransaction(request, {
            resolveModifier: name => keyval(modifierNames[name]),
            resolveKey: keyval,
            pressKey: key => this._keyboard.notify_keyval(now(), key, Clutter.KeyState.PRESSED),
            releaseKey: key => this._keyboard.notify_keyval(now(), key, Clutter.KeyState.RELEASED),
        });
        return {window: lease.target, key: request.key, modifiers: request.modifiers};
    }
}
