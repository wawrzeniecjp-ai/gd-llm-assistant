#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <cocos2d.h>

#include <optional>

namespace gdai::editor {

/// A rectangle in **level** coordinates -- the same space objects are placed in.
///
/// Sorted on the way out (x1 <= x2, y1 <= y2) so a caller never has to wonder which
/// corner is which.
struct LevelRect {
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 0.0;
    double y2 = 0.0;
};

/// Converts a rectangle in screen points into the level coordinates it covers.
///
/// Through the object layer's transform for the same reason the camera readings are:
/// that node is what maps level space onto the screen, so asking it is exact at any pan
/// and zoom. Corners are sorted because a transform with a negative scale -- which the
/// editor does not use for zoom today -- would otherwise hand back an inverted rect.
LevelRect screenRectToLevel(cocos2d::CCRect const& rect);

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
    /// The rectangle of the level currently on screen, in level coordinates.
    ///
    /// The centre and the zoom together *imply* this, but only if the caller also knows
    /// the window size and the visible band's height, which it has no way to learn. So it
    /// is reported directly: "these objects are on screen" is a question a caller asks
    /// about a rectangle, and one it should not have to reconstruct from a point.
    LevelRect visible;
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
