#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/desktop/Workspace.hpp>
#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/view/WLSurface.hpp>
#include <hyprland/src/helpers/Monitor.hpp>
#include <hyprland/src/managers/PointerManager.hpp>
#include <hyprland/src/managers/SeatManager.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/managers/input/InputManager.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>
#include <hyprland/src/protocols/core/DataDevice.hpp>
#include <hyprland/src/helpers/time/Time.hpp>
#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <format>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef CU_PLUGIN_VERSION
#define CU_PLUGIN_VERSION "unknown"
#endif
#ifndef CU_SOURCE_SHA256
#define CU_SOURCE_SHA256 "unknown"
#endif
#ifndef CU_HYPRLAND_BUILD_SHA256
#define CU_HYPRLAND_BUILD_SHA256 "unknown"
#endif

inline HANDLE PHANDLE = nullptr;

namespace {

struct ParsedRequest {
    std::string action;
    std::string identity;
    uintptr_t   address = 0;
    double      x1 = 0;
    double      y1 = 0;
    double      x2 = 0;
    double      y2 = 0;
    std::string button = "left";
    int         amount = 1;
};

struct ParsedBatch {
    std::string               identity;
    uintptr_t                 address = 0;
    std::vector<ParsedRequest> actions;
};

constexpr int BATCH_PROTOCOL_VERSION = 1;

struct PhysicalState {
    uintptr_t address = 0;
    int64_t   workspace = WORKSPACE_INVALID;
    Vector2D  cursor;
};

std::string jsonError(const std::string& message) {
    std::string escaped;
    escaped.reserve(message.size());
    for (const char ch : message) {
        if (ch == '"' || ch == '\\') escaped.push_back('\\');
        if (ch == '\n') escaped += "\\n";
        else escaped.push_back(ch);
    }
    return std::format("{{\"ok\":false,\"error\":\"{}\"}}", escaped);
}

std::string identityToken() {
    return std::format("v1.{}.{}.{}", CU_PLUGIN_VERSION, CU_SOURCE_SHA256, CU_HYPRLAND_BUILD_SHA256);
}

std::string identityJson() {
    return std::format(
        "{{\"plugin_version\":\"{}\",\"source_sha256\":\"{}\",\"hyprland_build_sha256\":\"{}\",\"hyprland_build_abi\":\"{}\",\"hyprland_runtime_abi\":\"{}\"}}",
        CU_PLUGIN_VERSION, CU_SOURCE_SHA256, CU_HYPRLAND_BUILD_SHA256, __hyprland_api_get_client_hash(), __hyprland_api_get_hash());
}

std::string physicalStateJson(const PhysicalState& state) {
    const auto address = state.address == 0 ? std::string{"null"} : std::format("\"0x{:x}\"", state.address);
    const auto workspace = state.workspace == WORKSPACE_INVALID ? std::string{"null"} : std::to_string(state.workspace);
    return std::format("{{\"active_address\":{},\"workspace\":{},\"cursor\":{{\"x\":{},\"y\":{}}}}}", address, workspace,
                       static_cast<int>(state.cursor.x), static_cast<int>(state.cursor.y));
}

std::string jsonOk(const ParsedRequest& req, const Vector2D& local, const std::string& surfaceKind, const PhysicalState& before,
                   const PhysicalState& after) {
    return std::format(
        "{{\"ok\":true,\"identity\":{},\"action\":\"{}\",\"address\":\"0x{:x}\",\"local_x\":{:.3f},\"local_y\":{:.3f},\"surface\":\"{}\",\"cursor_moved\":false,\"keyboard_focus_changed\":false,\"observed_physical_state_unchanged\":true,\"physical_state_before\":{},\"physical_state_after\":{},\"cursor_moved_by_backend\":false,\"keyboard_focus_changed_by_backend\":false,\"workspace_changed_by_backend\":false}}",
        identityJson(), req.action, req.address, local.x, local.y, surfaceKind, physicalStateJson(before), physicalStateJson(after));
}

bool parseAddress(const std::string& value, uintptr_t& output) {
    auto text = value;
    if (text.starts_with("0x") || text.starts_with("0X")) text.erase(0, 2);
    if (text.empty()) return false;
    const auto [ptr, ec] = std::from_chars(text.data(), text.data() + text.size(), output, 16);
    return ec == std::errc{} && ptr == text.data() + text.size() && output != 0;
}

bool parseDouble(const std::string& value, double& output) {
    try {
        size_t consumed = 0;
        output = std::stod(value, &consumed);
        return consumed == value.size() && std::isfinite(output);
    } catch (...) { return false; }
}

bool parseInt(const std::string& value, int& output) {
    const auto [ptr, ec] = std::from_chars(value.data(), value.data() + value.size(), output);
    return ec == std::errc{} && ptr == value.data() + value.size();
}

std::vector<std::string> words(const std::string& request) {
    std::istringstream stream(request);
    std::vector<std::string> values;
    for (std::string value; stream >> value;) values.push_back(value);
    return values;
}

bool parseRequest(const std::string& request, ParsedRequest& parsed, std::string& error) {
    auto args = words(request);
    if (!args.empty() && args.front() == "cutarget") args.erase(args.begin());
    if (args.size() < 5) {
        error = "usage: cutarget click|scroll|drag IDENTITY ADDRESS X Y [options]";
        return false;
    }
    parsed.action = args[0];
    parsed.identity = args[1];
    if (!parseAddress(args[2], parsed.address) || !parseDouble(args[3], parsed.x1) || !parseDouble(args[4], parsed.y1)) {
        error = "invalid address or coordinate";
        return false;
    }
    if (parsed.action == "click") {
        if (args.size() > 5) parsed.button = args[5];
        if (args.size() > 6 && !parseInt(args[6], parsed.amount)) {
            error = "invalid click count";
            return false;
        }
        if (args.size() > 7 || parsed.amount < 1 || parsed.amount > 3 ||
            (parsed.button != "left" && parsed.button != "right" && parsed.button != "middle")) {
            error = "click expects IDENTITY ADDRESS X Y [left|right|middle] [1..3]";
            return false;
        }
    } else if (parsed.action == "scroll") {
        if (args.size() != 6 || !parseInt(args[5], parsed.amount) || parsed.amount == 0 || std::abs(parsed.amount) > 20) {
            error = "scroll expects IDENTITY ADDRESS X Y STEPS (-20..20, excluding 0)";
            return false;
        }
    } else if (parsed.action == "drag") {
        if (args.size() < 7 || args.size() > 9 || !parseDouble(args[5], parsed.x2) || !parseDouble(args[6], parsed.y2)) {
            error = "drag expects IDENTITY ADDRESS START_X START_Y END_X END_Y [left|right|middle] [2..32 motion steps]";
            return false;
        }
        if (args.size() > 7) parsed.button = args[7];
        parsed.amount = 8;
        if (args.size() > 8 && !parseInt(args[8], parsed.amount)) {
            error = "invalid drag motion step count";
            return false;
        }
        if (parsed.amount < 2 || parsed.amount > 32 ||
            (parsed.button != "left" && parsed.button != "right" && parsed.button != "middle")) {
            error = "invalid drag button or motion step count";
            return false;
        }
    } else {
        error = "unknown action; expected click, scroll, or drag";
        return false;
    }
    return true;
}

bool parseBatchRequest(const std::string& request, ParsedBatch& parsed, std::string& error) {
    auto args = words(request);
    if (!args.empty() && args.front() == "cutargetbatch") args.erase(args.begin());
    if (args.size() < 5 || args[0] != "v1" || !parseAddress(args[2], parsed.address)) {
        error = "usage: cutargetbatch v1 IDENTITY ADDRESS COUNT ACTION...";
        return false;
    }
    parsed.identity = args[1];
    int count = 0;
    if (!parseInt(args[3], count) || count < 1 || count > 8) {
        error = "batch action count must be between 1 and 8";
        return false;
    }
    size_t offset = 4;
    for (int index = 0; index < count; ++index) {
        if (offset >= args.size()) {
            error = "batch ended before its declared action count";
            return false;
        }
        ParsedRequest action;
        action.action = args[offset++];
        action.identity = parsed.identity;
        action.address = parsed.address;
        const auto require = [&](size_t fields) { return offset + fields <= args.size(); };
        if (action.action == "click") {
            if (!require(4) || !parseDouble(args[offset], action.x1) || !parseDouble(args[offset + 1], action.y1) ||
                !parseInt(args[offset + 3], action.amount)) {
                error = "batch click expects X Y BUTTON COUNT";
                return false;
            }
            action.button = args[offset + 2];
            offset += 4;
            if (action.amount < 1 || action.amount > 3 ||
                (action.button != "left" && action.button != "right" && action.button != "middle")) {
                error = "batch click button or count is outside supported bounds";
                return false;
            }
        } else if (action.action == "scroll") {
            if (!require(3) || !parseDouble(args[offset], action.x1) || !parseDouble(args[offset + 1], action.y1) ||
                !parseInt(args[offset + 2], action.amount) || action.amount == 0 || std::abs(action.amount) > 20) {
                error = "batch scroll expects X Y STEPS";
                return false;
            }
            offset += 3;
        } else if (action.action == "drag") {
            if (!require(6) || !parseDouble(args[offset], action.x1) || !parseDouble(args[offset + 1], action.y1) ||
                !parseDouble(args[offset + 2], action.x2) || !parseDouble(args[offset + 3], action.y2) ||
                !parseInt(args[offset + 5], action.amount)) {
                error = "batch drag expects START_X START_Y END_X END_Y BUTTON MOTION_STEPS";
                return false;
            }
            action.button = args[offset + 4];
            offset += 6;
            if (action.amount < 2 || action.amount > 32 ||
                (action.button != "left" && action.button != "right" && action.button != "middle")) {
                error = "batch drag button or motion step count is outside supported bounds";
                return false;
            }
        } else {
            error = "unknown batch action; expected click, scroll, or drag";
            return false;
        }
        parsed.actions.push_back(std::move(action));
    }
    if (offset != args.size()) {
        error = "batch contains trailing fields after its declared actions";
        return false;
    }
    return true;
}

PhysicalState physicalState() {
    PhysicalState state;
    const auto    focus = Desktop::focusState();
    if (focus) {
        const auto window = focus->window();
        if (Desktop::View::validMapped(window)) state.address = reinterpret_cast<uintptr_t>(window.get());
        const auto monitor = focus->monitor();
        if (monitor && valid(monitor->m_activeWorkspace)) state.workspace = monitor->m_activeWorkspace->m_id;
    }
    state.cursor = g_pInputManager->getMouseCoordsInternal().floor();
    return state;
}

std::string physicalStateError(const ParsedRequest& request, const PhysicalState& before, const PhysicalState& after) {
    const bool cursorChanged = before.cursor.x != after.cursor.x || before.cursor.y != after.cursor.y;
    const bool focusChanged = before.address != after.address;
    const bool workspaceChanged = before.workspace != after.workspace;
    return jsonError(std::format(
        "targeted pointer {} did not preserve physical desktop state: changes={{\"cursor_moved_by_backend\":{},\"keyboard_focus_changed_by_backend\":{},\"workspace_changed_by_backend\":{}}}; before={}; after={}",
        request.action, cursorChanged, focusChanged, workspaceChanged, physicalStateJson(before), physicalStateJson(after)));
}

PHLWINDOW findWindow(uintptr_t address) {
    for (const auto& window : g_pCompositor->m_windows) {
        if (window && reinterpret_cast<uintptr_t>(window.get()) == address) return window;
    }
    return nullptr;
}

uint32_t buttonCode(const std::string& button) {
    if (button == "right") return 273;
    if (button == "middle") return 274;
    return 272;
}

Vector2D localForSurface(SP<CWLSurfaceResource> surface, const Vector2D& cursor) {
    if (!surface) return {};
    if (const auto window = g_pCompositor->getWindowFromSurface(surface))
        return g_pCompositor->vectorToSurfaceLocal(cursor, window, surface);
    const auto wrapper = Desktop::View::CWLSurface::fromResource(surface);
    if (!wrapper) return {};
    const auto box = wrapper->getSurfaceBoxGlobal();
    if (!box) return {};
    return cursor - Vector2D{box->x, box->y};
}

class PointerFocusRestore {
  public:
    PointerFocusRestore() : surface(g_pSeatManager->m_state.pointerFocus.lock()), cursor(g_pPointerManager->position()), local(localForSurface(surface, cursor)) {}
    ~PointerFocusRestore() {
        const auto now = static_cast<uint32_t>(Time::millis(Time::steadyNow()));
        g_pSeatManager->setPointerFocus(surface, local);
        if (surface) {
            g_pSeatManager->sendPointerMotion(now, local);
            g_pSeatManager->sendPointerFrame();
        }
    }

  private:
    SP<CWLSurfaceResource> surface;
    Vector2D               cursor;
    Vector2D               local;
};

struct TargetPoint {
    SP<CWLSurfaceResource> surface;
    Vector2D               local;
    std::string            kind;
};

TargetPoint resolveTarget(PHLWINDOW window, double x, double y, bool forceMain = false) {
    const auto size = window->m_realSize->goal();
    if (x < 0 || y < 0 || x >= size.x || y >= size.y)
        throw std::runtime_error(std::format("coordinate ({:.1f},{:.1f}) is outside window size {:.1f}x{:.1f}", x, y, size.x, size.y));

    const Vector2D local{x, y};
    if (window->m_isX11 || forceMain)
        return {window->wlSurface()->resource(), local, window->m_isX11 ? "xwayland" : "main"};

    const auto global = window->m_realPosition->goal() + local;
    Vector2D surfaceLocal;
    auto surface = g_pCompositor->vectorWindowToSurface(global, window, surfaceLocal);
    if (!surface) throw std::runtime_error("no input surface exists at the requested window coordinate");
    return {surface, surfaceLocal, surface == window->wlSurface()->resource() ? "main" : "subsurface"};
}

void ensureSafeToInject(PHLWINDOW window) {
    if (!window || !Desktop::View::validMapped(window)) throw std::runtime_error("target window is not mapped");
    if (!window->acceptsInput()) throw std::runtime_error("target window does not currently accept input");
    if (!g_pSeatManager || !g_pSeatManager->m_mouse) throw std::runtime_error("Hyprland has no active pointer seat");
    if (g_pSessionLockManager && g_pSessionLockManager->isSessionLocked()) throw std::runtime_error("session is locked");
    if (g_pInputManager->hasHeldButtons()) throw std::runtime_error("physical pointer button is currently held");
    if (g_pInputManager->isConstrained() || g_pInputManager->isLocked()) throw std::runtime_error("physical pointer is constrained or locked");
    if (PROTO::data && PROTO::data->dndActive()) throw std::runtime_error("a drag-and-drop operation is active");
}

TargetPoint injectPointerAction(const ParsedRequest& parsed, PHLWINDOW window) {
    const auto now = static_cast<uint32_t>(Time::millis(Time::steadyNow()));
    const auto start = resolveTarget(window, parsed.x1, parsed.y1, parsed.action == "drag");
    g_pSeatManager->setPointerFocus(start.surface, start.local);
    g_pSeatManager->sendPointerMotion(now, start.local);
    g_pSeatManager->sendPointerFrame();

    if (parsed.action == "click") {
        const auto button = buttonCode(parsed.button);
        for (int i = 0; i < parsed.amount; ++i) {
            g_pSeatManager->sendPointerButton(now, button, WL_POINTER_BUTTON_STATE_PRESSED);
            g_pSeatManager->sendPointerFrame();
            g_pSeatManager->sendPointerButton(now, button, WL_POINTER_BUTTON_STATE_RELEASED);
            g_pSeatManager->sendPointerFrame();
        }
    } else if (parsed.action == "scroll") {
        const auto value120 = parsed.amount * 120;
        g_pSeatManager->sendPointerAxis(now, WL_POINTER_AXIS_VERTICAL_SCROLL, static_cast<double>(parsed.amount) * 15.0,
                                        parsed.amount, value120, WL_POINTER_AXIS_SOURCE_WHEEL,
                                        WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL);
        g_pSeatManager->sendPointerFrame();
    } else if (parsed.action == "drag") {
        const auto button = buttonCode(parsed.button);
        g_pSeatManager->sendPointerButton(now, button, WL_POINTER_BUTTON_STATE_PRESSED);
        g_pSeatManager->sendPointerFrame();
        for (int i = 1; i <= parsed.amount; ++i) {
            const double t = static_cast<double>(i) / parsed.amount;
            const Vector2D local{parsed.x1 + (parsed.x2 - parsed.x1) * t, parsed.y1 + (parsed.y2 - parsed.y1) * t};
            g_pSeatManager->sendPointerMotion(now, local);
            g_pSeatManager->sendPointerFrame();
        }
        g_pSeatManager->sendPointerButton(now, button, WL_POINTER_BUTTON_STATE_RELEASED);
        g_pSeatManager->sendPointerFrame();
    }
    return start;
}

std::string handleStatus(eHyprCtlOutputFormat, std::string) {
    const bool pointerSeat = g_pSeatManager && g_pSeatManager->m_mouse;
    const bool inputManager = static_cast<bool>(g_pInputManager);
    const bool sessionLocked = g_pSessionLockManager && g_pSessionLockManager->isSessionLocked();
    const bool heldButtons = inputManager && g_pInputManager->hasHeldButtons();
    const bool pointerConstrained = inputManager && g_pInputManager->isConstrained();
    const bool pointerLocked = inputManager && g_pInputManager->isLocked();
    const bool dndActive = PROTO::data && PROTO::data->dndActive();
    const bool safe = pointerSeat && inputManager && !sessionLocked && !heldButtons && !pointerConstrained && !pointerLocked && !dndActive;
    return std::format(
        "{{\"ok\":true,\"plugin_version\":\"{}\",\"source_sha256\":\"{}\",\"hyprland_build_sha256\":\"{}\",\"hyprland_build_abi\":\"{}\",\"hyprland_runtime_abi\":\"{}\",\"batch_protocol_version\":{},\"identity_token\":\"{}\",\"safe_to_inject\":{},\"pointer_seat\":{},\"session_locked\":{},\"held_buttons\":{},\"pointer_constrained\":{},\"pointer_locked\":{},\"dnd_active\":{}}}",
        CU_PLUGIN_VERSION, CU_SOURCE_SHA256, CU_HYPRLAND_BUILD_SHA256, __hyprland_api_get_client_hash(), __hyprland_api_get_hash(),
        BATCH_PROTOCOL_VERSION, identityToken(), safe, pointerSeat, sessionLocked, heldButtons, pointerConstrained, pointerLocked, dndActive);
}

std::string handleRequest(eHyprCtlOutputFormat, std::string request) {
    try {
        ParsedRequest parsed;
        std::string error;
        if (!parseRequest(request, parsed, error)) return jsonError(error);
        if (parsed.identity != identityToken()) return jsonError("native input transaction identity does not match this broker");
        if (std::string{__hyprland_api_get_client_hash()} != std::string{__hyprland_api_get_hash()})
            return jsonError("native input transaction Hyprland ABI does not match the runtime");
        const auto window = findWindow(parsed.address);
        ensureSafeToInject(window);

        if (window->m_isX11) throw std::runtime_error("XWayland targets must use the same-session broker's XTEST route");

        if (parsed.action == "drag") resolveTarget(window, parsed.x2, parsed.y2, true);

        const auto before = physicalState();
        TargetPoint start;
        {
            PointerFocusRestore restore;
            start = injectPointerAction(parsed, window);
        }

        const auto after = physicalState();
        if (before.address != after.address || before.workspace != after.workspace || before.cursor.x != after.cursor.x ||
            before.cursor.y != after.cursor.y)
            return physicalStateError(parsed, before, after);
        return jsonOk(parsed, start.local, start.kind, before, after);
    } catch (const std::exception& exception) { return jsonError(exception.what()); }
}

std::string handleBatchRequest(eHyprCtlOutputFormat, std::string request) {
    try {
        ParsedBatch parsed;
        std::string error;
        if (!parseBatchRequest(request, parsed, error)) return jsonError(error);
        if (parsed.identity != identityToken()) return jsonError("native input transaction identity does not match this broker");
        if (std::string{__hyprland_api_get_client_hash()} != std::string{__hyprland_api_get_hash()})
            return jsonError("native input transaction Hyprland ABI does not match the runtime");
        const auto window = findWindow(parsed.address);
        ensureSafeToInject(window);
        if (window->m_isX11) throw std::runtime_error("XWayland targets are not supported by the native batch ABI");

        for (const auto& action : parsed.actions) {
            resolveTarget(window, action.x1, action.y1, action.action == "drag");
            if (action.action == "drag") resolveTarget(window, action.x2, action.y2, true);
        }

        const auto before = physicalState();
        {
            PointerFocusRestore restore;
            for (const auto& action : parsed.actions) injectPointerAction(action, window);
        }
        const auto after = physicalState();
        if (before.address != after.address || before.workspace != after.workspace || before.cursor.x != after.cursor.x ||
            before.cursor.y != after.cursor.y)
            return jsonError(std::format("targeted pointer batch did not preserve physical desktop state: before={}; after={}",
                                         physicalStateJson(before), physicalStateJson(after)));
        return std::format(
            "{{\"ok\":true,\"batch_protocol_version\":{},\"identity\":{},\"address\":\"0x{:x}\",\"completed\":{},\"observed_physical_state_unchanged\":true,\"physical_state_before\":{},\"physical_state_after\":{}}}",
            BATCH_PROTOCOL_VERSION, identityJson(), parsed.address, parsed.actions.size(), physicalStateJson(before),
            physicalStateJson(after));
    } catch (const std::exception& exception) { return jsonError(exception.what()); }
}

} // namespace

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    PHANDLE = handle;
    if (std::string{__hyprland_api_get_hash()} != std::string{__hyprland_api_get_client_hash()})
        throw std::runtime_error("same-session-target-pointer: Hyprland header/runtime version mismatch");

    const auto command = HyprlandAPI::registerHyprCtlCommand(
        PHANDLE, SHyprCtlCommand{.name = "cutarget", .exact = false, .fn = handleRequest});
    if (!command) throw std::runtime_error("same-session-target-pointer: failed to register cutarget command");

    const auto batchCommand = HyprlandAPI::registerHyprCtlCommand(
        PHANDLE, SHyprCtlCommand{.name = "cutargetbatch", .exact = false, .fn = handleBatchRequest});
    if (!batchCommand) throw std::runtime_error("same-session-target-pointer: failed to register cutargetbatch command");

    const auto statusCommand = HyprlandAPI::registerHyprCtlCommand(
        PHANDLE, SHyprCtlCommand{.name = "cutargetstatus", .exact = true, .fn = handleStatus});
    if (!statusCommand) throw std::runtime_error("same-session-target-pointer: failed to register cutargetstatus command");

    return {"same-session-target-pointer", "Atomic window-targeted pointer events without cursor movement", "Gabe", CU_PLUGIN_VERSION};
}

APICALL EXPORT void PLUGIN_EXIT() {}
