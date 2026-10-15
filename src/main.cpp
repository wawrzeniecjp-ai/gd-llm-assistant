#include <Geode/Geode.hpp>

using namespace geode::prelude;

// Phase 0 / spike S1: prove the toolchain builds and the mod loads.
$on_mod(Loaded) {
    log::info("GD AI Assistant {} loaded", Mod::get()->getVersion().toVString());
}
