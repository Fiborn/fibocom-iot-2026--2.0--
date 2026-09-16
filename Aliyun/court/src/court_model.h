#ifndef COURT_MODEL_H
#define COURT_MODEL_H

#include "obj_exporter.h"
#include <glm/glm.hpp>

// ============================================================
// BWF Standard Badminton Court Dimensions (meters)
// ============================================================
constexpr float COURT_LENGTH      = 13.40f;   // baseline to baseline
constexpr float COURT_WIDTH       = 6.10f;    // doubles sideline to sideline
constexpr float SINGLES_WIDTH     = 5.18f;    // singles sideline to sideline
constexpr float HALF_LENGTH       = 6.70f;    // net to baseline
constexpr float SHORT_SERVICE_DIST = 1.98f;   // short service line from net
constexpr float DOUBLES_SRV_OFFSET = 0.76f;   // doubles long service line from baseline

// Line widths
constexpr float LINE_WIDTH        = 0.025f;   // 25mm = 2.5cm
constexpr float GROUND_HEIGHT     = 0.010f;   // 10mm = 1cm thickness

// ============================================================
// Net dimensions
// ============================================================
constexpr float NET_HEIGHT_EDGE   = 1.55f;
constexpr float NET_HEIGHT_CENTER = 1.524f;
constexpr float NET_LENGTH        = 6.10f;    // full doubles court width (post to post)
constexpr float NET_HALF_LEN      = NET_LENGTH * 0.5f;
constexpr float TOP_BAND_HEIGHT   = 0.075f;   // 75mm
constexpr float NET_BOTTOM_GAP    = 0.79f;    // BWF net mesh depth 760mm: net bottom = 1.55 - 0.76 = 0.79m

// Posts
constexpr float POST_RADIUS       = 0.010f;   // 10mm = 1cm radius (2cm diameter)
constexpr float POST_HEIGHT       = NET_HEIGHT_EDGE;  // same as net edge (1.55m)
constexpr float POST_X_POS        = 3.05f;   // post at doubles sideline

class CourtModel {
public:
    CourtModel();

    // Ground as solid 1cm-thick rectangular slab
    ObjMesh generateGround();

    // All court lines as solid 2.5cm-wide x 1cm-thick rectangles
    void appendLines(ObjMesh& mesh);

private:
    void addBox(ObjMesh& m, float x1, float y1, float x2, float y2, float z1, float z2);
    void addLine3D(ObjMesh& m, float x1, float y1, float x2, float y2, float w, float z);
};

#endif
