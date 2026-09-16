/**
 * court_lines.cpp — Badminton court line mesh generation & per-line OBJ export
 *
 * Generates 13 BWF-standard court lines as 3D prisms (2.5cm wide x 1cm thick).
 * Each line is exported as a separate .obj file into outputDir.
 *
 * Line inventory (13 total):
 *
 *   Doubles sidelines  (x2): X=+/-3.05, from Y=-6.70 to Y=+6.70
 *   Baselines          (x2): Y=+/-6.70, from X=-3.05 to X=+3.05
 *   Singles sidelines  (x2): X=+/-2.59, from Y=-6.70 to Y=+6.70
 *   Center lines       (x2): X=0, from Y=+/-1.98 to Y=+/-6.70
 *   Short service lines(x2): Y=+/-1.98, from X=-3.05 to X=+3.05
 *   Long service lines (x2): Y=+/-5.94, from X=-3.05 to X=+3.05
 *   Net line           (x1): Y=0, from X=-3.05 to X=+3.05
 *
 * Build: part of court_generator project, via CMakeLists.txt
 * Output: F:\animation_final\court\output\court_line_*.obj
 */

#include "obj_exporter.h"
#include <glm/glm.hpp>
#include <iostream>
#include <string>
#include <cmath>

// ============================================================
// BWF standard dimensions (meters)
// ============================================================

constexpr float COURT_HALF_WIDTH  = 3.05f;   // doubles half-width
constexpr float COURT_HALF_LENGTH = 6.70f;   // half-court length (net to baseline)
constexpr float SINGLES_HALF_WIDTH = 2.59f;  // singles half-width
constexpr float SHORT_SERVICE_DIST = 1.98f;  // short service line distance from net
constexpr float LONG_SERVICE_OFFSET = 0.76f; // doubles long service line offset from baseline
constexpr float LONG_SERVICE_DIST = COURT_HALF_LENGTH - LONG_SERVICE_OFFSET; // 5.94m
constexpr float LINE_WIDTH = 0.025f;          // line width 2.5cm
constexpr float LINE_THICKNESS = 0.010f;      // line thickness 1cm

// ============================================================
// addLine3D — Generate one 3D line prism (2.5cm wide x 1cm thick)
// ============================================================
// 8 vertices (4 top + 4 bottom), 12 triangles (top, bottom, 4 sides, 2 end caps)
//   d = normalized direction (x2-x1, y2-y1)
//   p = perpendicular (-d.y, d.x) = left-side normal
static void addLine3D(ObjMesh& m, float x1, float y1, float x2, float y2, float w, float z) {
    glm::vec2 d(x2 - x1, y2 - y1);
    float len = glm::length(d);
    if (len < 0.0001f) return;  // skip zero-length segment

    d /= len;

    // p = left-side perpendicular, offset by +/- hw for line width
    glm::vec2 p(-d.y, d.x);
    float hw = w * 0.5f;
    float h  = LINE_THICKNESS;

    // Four corner points in XY plane (top layer z+h, bottom layer z)
    glm::vec2 c0(x1 + p.x * hw, y1 + p.y * hw); // start, +perp (left side)
    glm::vec2 c1(x2 + p.x * hw, y2 + p.y * hw); // end,   +perp
    glm::vec2 c2(x2 - p.x * hw, y2 - p.y * hw); // end,   -perp (right side)
    glm::vec2 c3(x1 - p.x * hw, y1 - p.y * hw); // start, -perp

    // 8 vertices: indices 0-3 top layer (z+h), 4-7 bottom layer (z)
    unsigned b = (unsigned)m.vertices.size();
    m.vertices.push_back(glm::vec3(c0.x, c0.y, z + h)); // 0: top, start, +perp
    m.vertices.push_back(glm::vec3(c1.x, c1.y, z + h)); // 1: top, end,   +perp
    m.vertices.push_back(glm::vec3(c2.x, c2.y, z + h)); // 2: top, end,   -perp
    m.vertices.push_back(glm::vec3(c3.x, c3.y, z + h)); // 3: top, start, -perp
    m.vertices.push_back(glm::vec3(c0.x, c0.y, z));     // 4: bottom, start, +perp
    m.vertices.push_back(glm::vec3(c1.x, c1.y, z));     // 5: bottom, end,   +perp
    m.vertices.push_back(glm::vec3(c2.x, c2.y, z));     // 6: bottom, end,   -perp
    m.vertices.push_back(glm::vec3(c3.x, c3.y, z));     // 7: bottom, start, -perp

    // Lambda: add one quadrilateral face (2 triangles) with given normal
    // Vertices a,b,c,d in CCW order (when viewed from normal direction)
    auto face = [&](int a, int b, int c, int d, glm::vec3 n) {
        for (int j = 0; j < 4; ++j) {
            m.normals.push_back(n);
            m.texcoords.push_back(glm::vec2(0, 0));
        }
        ObjMesh::Face f1, f2;
        f1.v[0] = (int)(b + a); f1.vt[0] = (int)(b + a); f1.vn[0] = (int)(b + a);
        f1.v[1] = (int)(b + b); f1.vt[1] = (int)(b + b); f1.vn[1] = (int)(b + b);
        f1.v[2] = (int)(b + c); f1.vt[2] = (int)(b + c); f1.vn[2] = (int)(b + c);
        f2.v[0] = (int)(b + a); f2.vt[0] = (int)(b + a); f2.vn[0] = (int)(b + a);
        f2.v[1] = (int)(b + c); f2.vt[1] = (int)(b + c); f2.vn[1] = (int)(b + c);
        f2.v[2] = (int)(b + d); f2.vt[2] = (int)(b + d); f2.vn[2] = (int)(b + d);
        m.faces.push_back(f1);
        m.faces.push_back(f2);
    };

    glm::vec3 nd(d.x, d.y, 0);   // along-line direction
    glm::vec3 np(p.x, p.y, 0);   // perpendicular (left-side normal)

    // 6 faces: bottom, top, +/-perp sides, start cap, end cap
    face(4, 5, 6, 7, glm::vec3(0, 0, -1)); // bottom (facing down)
    face(0, 1, 2, 3, glm::vec3(0, 0,  1)); // top (facing up)
    face(4, 0, 3, 7, -np);                  // -perp side (right side, inward)
    face(5, 1, 2, 6,  np);                  // +perp side (left side, outward)
    face(4, 7, 3, 0, -nd);                  // start end cap (all 4 verts at start)
    face(5, 6, 2, 1,  nd);                  // end end cap (all 4 verts at end)
}

// ============================================================
// generateAndExportLines — Generate all 13 lines and export each
// ============================================================
int generateAndExportLines(const std::string& outputDir, ObjMesh& combinedLines) {
    combinedLines.clear();
    float hw = COURT_HALF_WIDTH;       // 3.05
    float hl = COURT_HALF_LENGTH;      // 6.70
    float sw = SINGLES_HALF_WIDTH;     // 2.59
    float sv = SHORT_SERVICE_DIST;     // 1.98
    float ld = LONG_SERVICE_DIST;      // 5.94
    float lw = LINE_WIDTH;             // 2.5cm
    float z  = LINE_THICKNESS;         // line sits on top of ground

    int count = 0;

    // Helper: export one line mesh to OBJ, then merge into combinedLines
    auto exportAndMerge = [&](const char* filename, const char* label,
                              float x1, float y1, float x2, float y2,
                              const char* desc) {
        ObjMesh m;
        addLine3D(m, x1, y1, x2, y2, lw, z);
        ObjExporter::write(outputDir + "\\" + filename, m, "line_white.mtl");
        std::cout << "  " << label << "  " << desc
                  << " | " << m.vertices.size() << "v " << m.faces.size() << "f\n";

        // Merge into combined mesh (for OpenGL rendering)
        int vo = (int)combinedLines.vertices.size();
        int vto = (int)combinedLines.texcoords.size();
        int vno = (int)combinedLines.normals.size();
        combinedLines.vertices.insert(combinedLines.vertices.end(), m.vertices.begin(), m.vertices.end());
        combinedLines.texcoords.insert(combinedLines.texcoords.end(), m.texcoords.begin(), m.texcoords.end());
        combinedLines.normals.insert(combinedLines.normals.end(), m.normals.begin(), m.normals.end());
        for (const auto& f : m.faces) {
            ObjMesh::Face nf = f;
            for (int i = 0; i < 3; ++i) { nf.v[i] += vo; nf.vt[i] += vto; nf.vn[i] += vno; }
            combinedLines.faces.push_back(nf);
        }
        count++;
    };

    // -------------------------------------------------------
    //  1. Doubles right sideline — X=+3.05, full court length
    // -------------------------------------------------------
    exportAndMerge("court_line_01_doubles_right_sideline.obj",
                   "[01]", hw, -hl, hw, hl,
                   "Doubles right sideline  X=+3.05  Y=-6.70..+6.70");

    // -------------------------------------------------------
    //  2. Doubles left sideline — X=-3.05, full court length
    // -------------------------------------------------------
    exportAndMerge("court_line_02_doubles_left_sideline.obj",
                   "[02]", -hw, -hl, -hw, hl,
                   "Doubles left sideline   X=-3.05  Y=-6.70..+6.70");

    // -------------------------------------------------------
    //  3. Far baseline — Y=+6.70, full doubles width
    // -------------------------------------------------------
    exportAndMerge("court_line_03_far_baseline.obj",
                   "[03]", -hw, hl, hw, hl,
                   "Far baseline            Y=+6.70  X=-3.05..+3.05");

    // -------------------------------------------------------
    //  4. Near baseline — Y=-6.70, full doubles width
    // -------------------------------------------------------
    exportAndMerge("court_line_04_near_baseline.obj",
                   "[04]", -hw, -hl, hw, -hl,
                   "Near baseline           Y=-6.70  X=-3.05..+3.05");

    // -------------------------------------------------------
    //  5. Singles right sideline — X=+2.59, full court length
    //      (inset 0.46m from doubles sideline)
    // -------------------------------------------------------
    exportAndMerge("court_line_05_singles_right_sideline.obj",
                   "[05]", sw, -hl, sw, hl,
                   "Singles right sideline  X=+2.59  Y=-6.70..+6.70");

    // -------------------------------------------------------
    //  6. Singles left sideline — X=-2.59, full court length
    // -------------------------------------------------------
    exportAndMerge("court_line_06_singles_left_sideline.obj",
                   "[06]", -sw, -hl, -sw, hl,
                   "Singles left sideline   X=-2.59  Y=-6.70..+6.70");

    // -------------------------------------------------------
    //  7. Net line — Y=0, full doubles width
    //      (line marking net position on floor)
    // -------------------------------------------------------
    exportAndMerge("court_line_07_net_line.obj",
                   "[07]", -hw, 0, hw, 0,
                   "Net line                Y=0     X=-3.05..+3.05");

    // -------------------------------------------------------
    //  8. Far short service line — Y=+1.98, full doubles width
    //      (1.98m from net)
    // -------------------------------------------------------
    exportAndMerge("court_line_08_far_short_service_line.obj",
                   "[08]", -hw, sv, hw, sv,
                   "Far short service line  Y=+1.98 X=-3.05..+3.05");

    // -------------------------------------------------------
    //  9. Near short service line — Y=-1.98, full doubles width
    // -------------------------------------------------------
    exportAndMerge("court_line_09_near_short_service_line.obj",
                   "[09]", -hw, -sv, hw, -sv,
                   "Near short service line Y=-1.98 X=-3.05..+3.05");

    // -------------------------------------------------------
    // 10. Far doubles long service line — Y=+5.94, full doubles width
    //      (0.76m from baseline)
    // -------------------------------------------------------
    exportAndMerge("court_line_10_far_long_service_line.obj",
                   "[10]", -hw, ld, hw, ld,
                   "Far long service line   Y=+5.94 X=-3.05..+3.05");

    // -------------------------------------------------------
    // 11. Near doubles long service line — Y=-5.94
    // -------------------------------------------------------
    exportAndMerge("court_line_11_near_long_service_line.obj",
                   "[11]", -hw, -ld, hw, -ld,
                   "Near long service line  Y=-5.94 X=-3.05..+3.05");

    // -------------------------------------------------------
    // 12. Far center line — X=0, from Y=+1.98 to Y=+6.70
    //      (only between short service line and baseline)
    // -------------------------------------------------------
    exportAndMerge("court_line_12_far_center_line.obj",
                   "[12]", 0, sv, 0, hl,
                   "Far center line         X=0     Y=+1.98..+6.70");

    // -------------------------------------------------------
    // 13. Near center line — X=0, from Y=-6.70 to Y=-1.98
    // -------------------------------------------------------
    exportAndMerge("court_line_13_near_center_line.obj",
                   "[13]", 0, -hl, 0, -sv,
                   "Near center line        X=0     Y=-6.70..-1.98");

    // White material shared by all lines
    ObjExporter::writeMtl(outputDir + "\\line_white.mtl", glm::vec3(1.0f, 1.0f, 1.0f));

    std::cout << "\n  Exported " << count << " court lines to " << outputDir << "\\court_line_*.obj\n";
    return count;
}
