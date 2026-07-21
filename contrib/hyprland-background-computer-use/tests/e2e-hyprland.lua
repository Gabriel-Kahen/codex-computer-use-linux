hl.monitor({
    output = "",
    mode = "800x600@60",
    position = "0x0",
    scale = 1,
})

hl.config({
    animations = {
        enabled = false,
    },
    decoration = {
        blur = {
            enabled = false,
        },
        shadow = {
            enabled = false,
        },
    },
    misc = {
        disable_hyprland_logo = true,
        disable_splash_rendering = true,
    },
    xwayland = {
        enabled = false,
    },
})
