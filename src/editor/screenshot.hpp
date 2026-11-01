#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <cocos2d.h>

#include <string>

namespace gdai::editor {

/// What a capture covers (SPEC.md 7.5.6).
struct CaptureRequest {
    /// "screen" for the whole editor view, "selection" for the selected objects.
    std::string region = "screen";
    /// Padding around the selection, in points. Ignored for "screen".
    float radius = 120.f;
    /// Keep the editor's toolbar, buttons and tab bars in the image.
    ///
    /// Off by default, and it earns its place: the first live test had the model reporting
    /// on the UI -- the toolbars at the bottom, the buttons around the edge -- which is
    /// noise it has to filter before it can say anything useful about the level. Hidden is
    /// the useful default, and the option exists for the rare question about editor state.
    bool includeUi = false;
};

/// A written capture.
struct Capture {
    /// Absolute path to the PNG. The sidecar reads it; the file is one fixed name in the
    /// mod's save directory, rewritten on every capture, so captures never accumulate.
    std::string path;
    int width = 0;
    int height = 0;
    /// The region captured, in the editor's point space, as `x, y, w, h`.
    float x = 0.f;
    float y = 0.f;
    float regionWidth = 0.f;
    float regionHeight = 0.f;
    /// Objects the region was computed from; 0 for a whole-screen capture.
    int targets = 0;
};

/// Renders the editor view to a PNG. Main thread only.
///
/// **The path, not the bytes.** The image comes back as a file path for the sidecar to
/// read, rather than as base64 through the protocol. Three reasons, in order of
/// importance:
///
///  - The protocol's frame limit is 1 MB. A 512x320 PNG is often 100-200 KB, which base64
///    inflates by a third -- workable today and fragile the moment anything changes.
///  - It needs no base64 encoder or PNG plumbing in the mod. `CCImage::saveToFile` is the
///    game's own writer.
///  - Both processes are on the same machine by construction, so the file is not a
///    distributed-systems compromise; it is the natural shared medium.
///
/// The file is overwritten each time, so captures do not accumulate.
///
/// Rendering is done with `CCRenderTexture` and cropped by drawing the texture through a
/// sprite with a texture rect. The running scene is **not** moved: shifting it would be a
/// one-frame lie about the scene's position, and a crash in between would leave it shifted.
geode::Result<Capture> captureEditor(CaptureRequest const& request);

/// The current selection's bounds in point space, or empty when nothing is selected.
///
/// Exposed for the request handler so a "selection" capture with nothing selected can say
/// so, rather than silently capturing the whole screen.
geode::Result<cocos2d::CCRect> selectionBounds();

matjson::Value captureToJson(Capture const& capture);

} // namespace gdai::editor
