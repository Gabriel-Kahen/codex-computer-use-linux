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
    activateLeaseTransaction,
    assertInputSafe,
    injectKeyTransaction,
    injectPointerTransaction,
    restoreLeaseTransaction,
} from './shell_transactions.js';

const BUS_NAME = 'org.gnome.Shell.Extensions.BackgroundComputerUse';
const OBJECT_PATH = '/org/gnome/Shell/Extensions/BackgroundComputerUse';
const XML = `<node>
  <interface name="org.gnome.Shell.Extensions.BackgroundComputerUse">
    <method name="Status"><arg type="s" direction="out"/></method>
    <method name="ListWindows"><arg type="s" direction="out"/></method>
    <method name="BeginLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="ActivateLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RestoreLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RecoverLease"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="InjectPointer"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="InjectKeys"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
  </interface>
</node>`;

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
            shell_version: Config.PACKAGE_VERSION,
            locked: Main.sessionMode.isLocked,
            overview_visible: Main.overview.visible,
            modal_count: Main.modalCount,
            grab_active: global.display.is_grabbed(),
            lease_phase: this._protocol.lease?.phase ?? null,
            shell_instance: this._shellInstance,
        });
    }

    ListWindows() {
        return json(this._windows().map(window => this._window(window)));
    }

    BeginLeaseAsync([id], invocation) {
        this._reply(invocation, () => this._beginLease(id, invocation.get_sender()));
    }

    _beginLease(id, owner) {
        this._assertInputSafe();
        const window = this._find(id);
        const capability = `${GLib.uuid_string_random()}${GLib.uuid_string_random()}`;
        const lease = this._protocol.begin({
            capability,
            owner,
            target: id,
            targetMinimized: Boolean(window.minimized),
            original: this._state(),
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
            return this._restoreLease(this._protocol.recover(capability, sender, ownerPresent));
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
