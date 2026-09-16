#include "net_model.h"
#include "court_model.h"
#include <glm/glm.hpp>
constexpr float GLM_TWO_PI = 6.283185307f;
#include <iostream>
#include <cmath>

NetModel::NetModel() {}

float NetModel::netHeightAt(float x) const {
    // Parabolic droop: 1.524m at center, 1.55m at posts (2.6cm sag)
    float r = x / NET_HALF_LEN;
    return NET_HEIGHT_CENTER + (NET_HEIGHT_EDGE - NET_HEIGHT_CENTER) * r * r;
}

// Helper: add a thin thread quad at Y=0 (in the net plane)
// start/end are (x, z) positions of the thread centerline
// width: perpendicular thickness of the thread
static void addThreadQuad(ObjMesh& mesh, float x1, float z1, float x2, float z2,
                          float width, float x_min, float x_max, float z_max) {
    // Clip to net domain
    if (x1 < x_min || x2 > x_max) return;
    if (z1 > z_max || z2 > z_max) return;
    if (z1 < 0 || z2 < 0) return;

    glm::vec2 d(x2 - x1, z2 - z1);
    float len = glm::length(d);
    if (len < 0.0001f) return;
    d /= len;
    glm::vec2 perp(-d.y, d.x);
    float hw = width * 0.5f;

    unsigned int b = (unsigned int)mesh.vertices.size();

    // Four corners of the thread quad (in the Y=0 plane)
    mesh.vertices.push_back(glm::vec3(x1 + perp.x * hw, 0, z1 + perp.y * hw));
    mesh.vertices.push_back(glm::vec3(x2 + perp.x * hw, 0, z2 + perp.y * hw));
    mesh.vertices.push_back(glm::vec3(x2 - perp.x * hw, 0, z2 - perp.y * hw));
    mesh.vertices.push_back(glm::vec3(x1 - perp.x * hw, 0, z1 - perp.y * hw));

    glm::vec3 n(0, 1, 0);
    for (int i = 0; i < 4; ++i) { mesh.normals.push_back(n); mesh.texcoords.push_back(glm::vec2(0,0)); }

    ObjMesh::Face f1, f2;
    f1.v[0] = (int)(b+0); f1.vt[0] = (int)(b+0); f1.vn[0] = (int)(b+0);
    f1.v[1] = (int)(b+1); f1.vt[1] = (int)(b+1); f1.vn[1] = (int)(b+1);
    f1.v[2] = (int)(b+2); f1.vt[2] = (int)(b+2); f1.vn[2] = (int)(b+2);

    f2.v[0] = (int)(b+0); f2.vt[0] = (int)(b+0); f2.vn[0] = (int)(b+0);
    f2.v[1] = (int)(b+2); f2.vt[1] = (int)(b+2); f2.vn[1] = (int)(b+2);
    f2.v[2] = (int)(b+3); f2.vt[2] = (int)(b+3); f2.vn[2] = (int)(b+3);

    mesh.faces.push_back(f1);
    mesh.faces.push_back(f2);
}

ObjMesh NetModel::generateNet(int holes_x, int holes_y) {
    ObjMesh mesh;

    // Net construction using horizontal + vertical threads:
    //
    //   ┌────┬────┬────┬────┐   ← horizontal thread (solid)
    //   │    │    │    │    │
    //   ├────┼────┼────┼────┤   ← horizontal thread
    //   │    │    │    │    │
    //   ├────┼────┼────┼────┤   ← horizontal thread
    //   │    │    │    │    │
    //   └────┴────┴────┴────┘   ← bottom
    //   ↑ vertical threads
    //
    // Each short vertical line = a thin quad at that X position
    // Each horizontal line = a thin quad at that Z position
    // The cells between threads are empty (the holes)

    const float hole = HOLE_W;            // 19mm hole
    const float tw  = THREAD_W;           // 3mm thread
    const float pitch = hole + tw;        // 22mm center-to-center

    float hw = NET_HALF_LEN;              // half net width
    float max_h = NET_HEIGHT_EDGE;        // tallest point

    // Calculate thread counts
    int n_horiz = (int)(max_h / pitch) + 2;   // horizontal thread count
    int n_vert  = (int)(hw * 2.0f / pitch) + 2; // vertical thread count

    // ---- Horizontal threads ----
    // Each runs from X=-hw to X=+hw at a fixed Z
    for (int i = 0; i <= n_horiz; ++i) {
        float z = (float)i * pitch;
        if (z < NET_BOTTOM_GAP) continue;  // skip bottom gap
        if (z > max_h) break;

        // Check if this row is within the net's parabolic top at all X positions
        // The horizontal thread follows the parabolic curve at its Z level
        // Since the thread is horizontal at constant Z, we clip where Z > netHeight(X)
        float x1 = -hw;
        float x2 = hw;

        // Clip to where Z <= netHeight(X): Z <= 1.524 + (1.55-1.524)*(X/3.01)²
        // X where Z = netHeight(X): X = NET_HALF_LEN * sqrt((Z - 1.524)/(1.55-1.524))
        if (z > NET_HEIGHT_CENTER) {
            float ratio = (z - NET_HEIGHT_CENTER) / (NET_HEIGHT_EDGE - NET_HEIGHT_CENTER);
            float clip_x = NET_HALF_LEN * std::sqrt(ratio);
            x1 = std::max(x1, -clip_x);
            x2 = std::min(x2,  clip_x);
        }

        if (x2 - x1 < 0.001f) continue;

        addThreadQuad(mesh, x1, z, x2, z, tw, -hw, hw, max_h);
    }

    // ---- Vertical threads ----
    // Each runs from Z=0 to Z=netHeightAt(X) at a fixed X
    for (int i = 0; i <= n_vert; ++i) {
        float x = -hw + (float)i * pitch;
        if (x > hw) break;

        float z_top = netHeightAt(x);
        if (z_top < 0.001f) continue;

        if (z_top <= NET_BOTTOM_GAP) continue;  // entirely within bottom gap
        float z_bottom = NET_BOTTOM_GAP;
        addThreadQuad(mesh, x, z_bottom, x, z_top, tw, -hw, hw, max_h);
    }

    std::cout << "  Net: " << mesh.vertices.size() << " verts, "
              << mesh.faces.size() << " tris ("
              << (n_horiz+1) << " horiz × " << (n_vert+1) << " vert threads)\n";
    return mesh;
}

ObjMesh NetModel::generateTopBand() {
    ObjMesh mesh;
    int segs = 80;
    float xL = -NET_HALF_LEN;
    float dx = (NET_HALF_LEN * 2.0f) / (float)segs;

    for (int i = 0; i < segs; ++i) {
        float x1 = xL + (float)i * dx;
        float x2 = xL + (float)(i+1) * dx;
        float h1 = netHeightAt(x1);
        float h2 = netHeightAt(x2);
        float b1 = h1 - TOP_BAND_HEIGHT;
        float b2 = h2 - TOP_BAND_HEIGHT;

        unsigned int b = (unsigned int)mesh.vertices.size();
        mesh.vertices.push_back(glm::vec3(x1, 0, b1));
        mesh.vertices.push_back(glm::vec3(x2, 0, b2));
        mesh.vertices.push_back(glm::vec3(x2, 0, h2));
        mesh.vertices.push_back(glm::vec3(x1, 0, h1));

        glm::vec3 n(0, 1, 0);
        for (int j = 0; j < 4; ++j) { mesh.normals.push_back(n); mesh.texcoords.push_back(glm::vec2(0,0)); }

        ObjMesh::Face f1, f2;
        f1.v[0] = (int)(b+0); f1.vt[0] = (int)(b+0); f1.vn[0] = (int)(b+0);
        f1.v[1] = (int)(b+1); f1.vt[1] = (int)(b+1); f1.vn[1] = (int)(b+1);
        f1.v[2] = (int)(b+2); f1.vt[2] = (int)(b+2); f1.vn[2] = (int)(b+2);

        f2.v[0] = (int)(b+0); f2.vt[0] = (int)(b+0); f2.vn[0] = (int)(b+0);
        f2.v[1] = (int)(b+2); f2.vt[1] = (int)(b+2); f2.vn[1] = (int)(b+2);
        f2.v[2] = (int)(b+3); f2.vt[2] = (int)(b+3); f2.vn[2] = (int)(b+3);

        mesh.faces.push_back(f1);
        mesh.faces.push_back(f2);
    }

    std::cout << "  Top band: " << mesh.vertices.size() << " verts\n";
    return mesh;
}

// Generate posts as full cylinders centered on the net line
ObjMesh NetModel::generatePosts(int segs) {
    ObjMesh mesh;

    for (int side = 0; side < 2; ++side) {
        float cx = (side == 0) ? -POST_X_POS : POST_X_POS;
        float h = netHeightAt(cx);
        for (int i = 0; i < segs; ++i) {
            float a1 = (float)i / (float)segs * GLM_TWO_PI;
            float a2 = (float)(i+1) / (float)segs * GLM_TWO_PI;
            float ca1 = cos(a1), sa1 = sin(a1);
            float ca2 = cos(a2), sa2 = sin(a2);
            float x1 = cx + POST_RADIUS * ca1, y1 = POST_RADIUS * sa1;
            float x2 = cx + POST_RADIUS * ca2, y2 = POST_RADIUS * sa2;

            unsigned int b = (unsigned int)mesh.vertices.size();
            mesh.vertices.push_back(glm::vec3(x1, y1, 0));
            mesh.vertices.push_back(glm::vec3(x2, y2, 0));
            mesh.vertices.push_back(glm::vec3(x2, y2, h));
            mesh.vertices.push_back(glm::vec3(x1, y1, h));

            glm::vec3 na(ca1, sa1, 0), nb(ca2, sa2, 0);
            mesh.normals.push_back(na); mesh.normals.push_back(nb);
            mesh.normals.push_back(nb); mesh.normals.push_back(na);

            for (int j = 0; j < 4; ++j) mesh.texcoords.push_back(glm::vec2(0,0));

            ObjMesh::Face f1, f2;
            f1.v[0] = (int)(b+0); f1.vt[0] = (int)(b+0); f1.vn[0] = (int)(b+0);
            f1.v[1] = (int)(b+1); f1.vt[1] = (int)(b+1); f1.vn[1] = (int)(b+1);
            f1.v[2] = (int)(b+2); f1.vt[2] = (int)(b+2); f1.vn[2] = (int)(b+2);

            f2.v[0] = (int)(b+0); f2.vt[0] = (int)(b+0); f2.vn[0] = (int)(b+0);
            f2.v[1] = (int)(b+2); f2.vt[1] = (int)(b+2); f2.vn[1] = (int)(b+2);
            f2.v[2] = (int)(b+3); f2.vt[2] = (int)(b+3); f2.vn[2] = (int)(b+3);

            mesh.faces.push_back(f1);
            mesh.faces.push_back(f2);
        }
    }

    std::cout << "  Posts: " << mesh.vertices.size() << " verts\n";
    return mesh;
}

// Generate and export left+right posts as separate .obj files
ObjMesh NetModel::generateAndExportPosts(const std::string& outputDir, int segs) {
    ObjMesh combined;

    for (int side = 0; side < 2; ++side) {
        const char* name = (side == 0) ? "net_post_left.obj" : "net_post_right.obj";
        const char* label = (side == 0) ? "Left post " : "Right post";
        float cx = (side == 0) ? -POST_X_POS : POST_X_POS;
        float h = netHeightAt(cx);

        ObjMesh m;
        for (int i = 0; i < segs; ++i) {
            float a1 = (float)i / (float)segs * GLM_TWO_PI;
            float a2 = (float)(i+1) / (float)segs * GLM_TWO_PI;
            float ca1 = cos(a1), sa1 = sin(a1);
            float ca2 = cos(a2), sa2 = sin(a2);
            float x1 = cx + POST_RADIUS * ca1, y1 = POST_RADIUS * sa1;
            float x2 = cx + POST_RADIUS * ca2, y2 = POST_RADIUS * sa2;

            unsigned int b = (unsigned int)m.vertices.size();
            m.vertices.push_back(glm::vec3(x1, y1, 0));
            m.vertices.push_back(glm::vec3(x2, y2, 0));
            m.vertices.push_back(glm::vec3(x2, y2, h));
            m.vertices.push_back(glm::vec3(x1, y1, h));

            glm::vec3 na(ca1, sa1, 0), nb(ca2, sa2, 0);
            m.normals.push_back(na); m.normals.push_back(nb);
            m.normals.push_back(nb); m.normals.push_back(na);

            for (int j = 0; j < 4; ++j) m.texcoords.push_back(glm::vec2(0,0));

            ObjMesh::Face f1, f2;
            f1.v[0] = (int)(b+0); f1.vt[0] = (int)(b+0); f1.vn[0] = (int)(b+0);
            f1.v[1] = (int)(b+1); f1.vt[1] = (int)(b+1); f1.vn[1] = (int)(b+1);
            f1.v[2] = (int)(b+2); f1.vt[2] = (int)(b+2); f1.vn[2] = (int)(b+2);

            f2.v[0] = (int)(b+0); f2.vt[0] = (int)(b+0); f2.vn[0] = (int)(b+0);
            f2.v[1] = (int)(b+2); f2.vt[1] = (int)(b+2); f2.vn[1] = (int)(b+2);
            f2.v[2] = (int)(b+3); f2.vt[2] = (int)(b+3); f2.vn[2] = (int)(b+3);

            m.faces.push_back(f1);
            m.faces.push_back(f2);
        }

        // Export individual post
        ObjExporter::write(outputDir + "\\" + name, m, "post.mtl");
        std::cout << "  " << label << " X=" << cx << " H=" << h << "m | "
                  << m.vertices.size() << "v " << m.faces.size() << "f\n";

        // Merge into combined mesh (for OpenGL rendering)
        int vo = (int)combined.vertices.size();
        int vto = (int)combined.texcoords.size();
        int vno = (int)combined.normals.size();
        combined.vertices.insert(combined.vertices.end(), m.vertices.begin(), m.vertices.end());
        combined.texcoords.insert(combined.texcoords.end(), m.texcoords.begin(), m.texcoords.end());
        combined.normals.insert(combined.normals.end(), m.normals.begin(), m.normals.end());
        for (const auto& f : m.faces) {
            ObjMesh::Face nf = f;
            for (int i = 0; i < 3; ++i) { nf.v[i] += vo; nf.vt[i] += vto; nf.vn[i] += vno; }
            combined.faces.push_back(nf);
        }
    }

    ObjExporter::writeMtl(outputDir + "\\post.mtl", glm::vec3(0.6f, 0.6f, 0.6f));
    std::cout << "  Exported net_post_left.obj + net_post_right.obj\n";
    return combined;
}

ObjMesh NetModel::generateCompleteNet(int holes_x, int holes_y) {
    ObjMesh net = generateNet(holes_x, holes_y);
    ObjMesh band = generateTopBand();

    int vo = (int)net.vertices.size();
    int vto = (int)net.texcoords.size();
    int vno = (int)net.normals.size();

    net.vertices.insert(net.vertices.end(), band.vertices.begin(), band.vertices.end());
    net.texcoords.insert(net.texcoords.end(), band.texcoords.begin(), band.texcoords.end());
    net.normals.insert(net.normals.end(), band.normals.begin(), band.normals.end());

    for (const auto& f : band.faces) {
        ObjMesh::Face nf = f;
        for (int i = 0; i < 3; ++i) {
            nf.v[i] += vo; nf.vt[i] += vto; nf.vn[i] += vno;
        }
        net.faces.push_back(nf);
    }
    return net;
}
