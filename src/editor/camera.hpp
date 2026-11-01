#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <optional>

namespace gdai::editor {

/// Where the editor is looking (SPEC.md 7.5.7).
struct Camera {
    /// The point at the centre of the view, in **level** coordinates.
    ///
    /// Level coordinates, not screen ones: the same space objects are placed in, so a
    /// caller can ask to look at something it just read from `query_objects` without
    /// converting anything.
    double x = 0.0;
    double y = 0.0;
    float zoom = 1.0f;
};

/// Reads the editor camera. Main thread only.
geode::Result<Camera> readCamera();

/// Moves and/or zooms the editor camera. Main thread only.
///
/// `x`/`y` are level coordinates and mean "centre the view here"; `zoom` is the editor's
/// own zoom factor, where 1 is the default framing.
///
/// **This moves the user's view.** That is the point -- without it the model can only see
/// whatever happens to be on screen -- but it is visible and it is not part of the level,
/// so a caller should say it is about to happen rather than silently dragging the view
/// around while the user is working.
///
/// Uses `EditorUI::centerCameraOnPoint` and `EditorUI::updateZoom` rather than writing
/// camera fields: those are what the editor's own pan and zoom controls call, so the view,
/// the scroll bars and the zoom label all stay consistent. Poking `m_gameState.m_cameraZoom`
/// would move the level without moving anything that reports where it is.
geode::Result<Camera> writeCamera(std::optional<double> x, std::optional<double> y, std::optional<float> zoom);

/// Serialises the camera for the protocol.
matjson::Value cameraToJson(Camera const& camera);

} // namespace gdai::editor
