#ifndef NET_MODEL_H
#define NET_MODEL_H

#include "obj_exporter.h"
#include "court_model.h"
#include <string>

class NetModel {
public:
    NetModel();

    // Generate net mesh (thread grid with diamond holes)
    ObjMesh generateNet(int holes_x = 140, int holes_y = 35);

    // Generate the 7.5cm white top band following the net's parabolic curve
    ObjMesh generateTopBand();

    // Generate both net posts as a single mesh (legacy, for combined export)
    ObjMesh generatePosts(int segments = 16);

    // Generate and export left + right posts as separate .obj files.
    // Returns combined mesh for OpenGL rendering.
    ObjMesh generateAndExportPosts(const std::string& outputDir, int segments = 32);

    // Generate all net components combined
    ObjMesh generateCompleteNet(int holes_x = 140, int holes_y = 35);

private:
    // Net top height at a given X position (parabola: 1.524m center -> 1.55m edges)
    float netHeightAt(float x) const;

    // Thread width (the solid material between holes)
    static constexpr float THREAD_W = 0.003f;   // 3mm thread thickness
    static constexpr float HOLE_W  = 0.019f;    // ~19mm hole size (BWF standard)
};

#endif
