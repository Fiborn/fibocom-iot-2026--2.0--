#include "court_model.h"
#include <glm/glm.hpp>
#include <iostream>

CourtModel::CourtModel() {}

// --- 3D box from (x1,y1,z1) to (x2,y2,z2), 6 faces with outward normals ---
void CourtModel::addBox(ObjMesh& m, float x1, float y1, float z1, float x2, float y2, float z2) {
    unsigned b = (unsigned)m.vertices.size();
    m.vertices.push_back(glm::vec3(x1, y1, z1)); // 0
    m.vertices.push_back(glm::vec3(x2, y1, z1)); // 1
    m.vertices.push_back(glm::vec3(x2, y2, z1)); // 2
    m.vertices.push_back(glm::vec3(x1, y2, z1)); // 3
    m.vertices.push_back(glm::vec3(x1, y1, z2)); // 4
    m.vertices.push_back(glm::vec3(x2, y1, z2)); // 5
    m.vertices.push_back(glm::vec3(x2, y2, z2)); // 6
    m.vertices.push_back(glm::vec3(x1, y2, z2)); // 7

    auto face = [&](int a, int b, int c, int d, glm::vec3 n) {
        for (int j = 0; j < 4; ++j) { m.normals.push_back(n); m.texcoords.push_back(glm::vec2(0,0)); }
        ObjMesh::Face f1, f2;
        f1.v[0]=(int)(b+a); f1.vt[0]=(int)(b+a); f1.vn[0]=(int)(b+a);
        f1.v[1]=(int)(b+b); f1.vt[1]=(int)(b+b); f1.vn[1]=(int)(b+b);
        f1.v[2]=(int)(b+c); f1.vt[2]=(int)(b+c); f1.vn[2]=(int)(b+c);
        f2.v[0]=(int)(b+a); f2.vt[0]=(int)(b+a); f2.vn[0]=(int)(b+a);
        f2.v[1]=(int)(b+c); f2.vt[1]=(int)(b+c); f2.vn[1]=(int)(b+c);
        f2.v[2]=(int)(b+d); f2.vt[2]=(int)(b+d); f2.vn[2]=(int)(b+d);
        m.faces.push_back(f1); m.faces.push_back(f2);
    };

    face(0,1,2,3, glm::vec3(0,0,-1)); // bottom
    face(4,5,6,7, glm::vec3(0,0,1));  // top
    face(3,2,6,7, glm::vec3(0,1,0));  // front (+y)
    face(0,1,5,4, glm::vec3(0,-1,0)); // back (-y)
    face(1,2,6,5, glm::vec3(1,0,0));  // right (+x)
    face(0,3,7,4, glm::vec3(-1,0,0)); // left (-x)
}

// --- 3D extruded line: rectangular prism, width w, height GROUND_HEIGHT ---
void CourtModel::addLine3D(ObjMesh& m, float x1, float y1, float x2, float y2, float w, float z) {
    glm::vec2 d(x2-x1, y2-y1);
    float len = glm::length(d);
    if (len < 0.0001f) return;
    d /= len;
    glm::vec2 p(-d.y, d.x);
    float hw = w * 0.5f, h = GROUND_HEIGHT;

    glm::vec2 c0(x1 + p.x*hw, y1 + p.y*hw);
    glm::vec2 c1(x2 + p.x*hw, y2 + p.y*hw);
    glm::vec2 c2(x2 - p.x*hw, y2 - p.y*hw);
    glm::vec2 c3(x1 - p.x*hw, y1 - p.y*hw);

    unsigned b = (unsigned)m.vertices.size();
    m.vertices.push_back(glm::vec3(c0.x, c0.y, z+h)); // 0 top +perp start
    m.vertices.push_back(glm::vec3(c1.x, c1.y, z+h)); // 1 top +perp end
    m.vertices.push_back(glm::vec3(c2.x, c2.y, z+h)); // 2 top -perp end
    m.vertices.push_back(glm::vec3(c3.x, c3.y, z+h)); // 3 top -perp start
    m.vertices.push_back(glm::vec3(c0.x, c0.y, z));   // 4 bottom
    m.vertices.push_back(glm::vec3(c1.x, c1.y, z));   // 5
    m.vertices.push_back(glm::vec3(c2.x, c2.y, z));   // 6
    m.vertices.push_back(glm::vec3(c3.x, c3.y, z));   // 7

    auto face = [&](int a, int b, int c, int d, glm::vec3 n) {
        for (int j = 0; j < 4; ++j) { m.normals.push_back(n); m.texcoords.push_back(glm::vec2(0,0)); }
        ObjMesh::Face f1, f2;
        f1.v[0]=(int)(b+a); f1.vt[0]=(int)(b+a); f1.vn[0]=(int)(b+a);
        f1.v[1]=(int)(b+b); f1.vt[1]=(int)(b+b); f1.vn[1]=(int)(b+b);
        f1.v[2]=(int)(b+c); f1.vt[2]=(int)(b+c); f1.vn[2]=(int)(b+c);
        f2.v[0]=(int)(b+a); f2.vt[0]=(int)(b+a); f2.vn[0]=(int)(b+a);
        f2.v[1]=(int)(b+c); f2.vt[1]=(int)(b+c); f2.vn[1]=(int)(b+c);
        f2.v[2]=(int)(b+d); f2.vt[2]=(int)(b+d); f2.vn[2]=(int)(b+d);
        m.faces.push_back(f1); m.faces.push_back(f2);
    };
    glm::vec3 nd(d.x, d.y, 0), np(p.x, p.y, 0);

    face(4,5,6,7, glm::vec3(0,0,-1));  // bottom
    face(0,1,2,3, glm::vec3(0,0,1));   // top
    face(4,0,3,7, -np);                // -perp side
    face(5,1,2,6, np);                 // +perp side
    face(4,7,3,0, -nd);                // end cap at start
    face(5,6,2,1, nd);                 // end end cap
}

// --- Ground: solid green slab, 1cm thick ---
ObjMesh CourtModel::generateGround() {
    ObjMesh m;
    float margin = 0.08f;
    float gx = COURT_WIDTH * 0.5f + margin;   // 3.13
    float gy = HALF_LENGTH + margin;           // 6.78
    addBox(m, -gx, -gy, 0.0f, gx, gy, GROUND_HEIGHT);
    std::cout << "  Ground: " << (gx*2) << "m x " << (gy*2) << "m x " << GROUND_HEIGHT << "m slab\n";
    return m;
}

// --- All court lines: 3D prisms, 2.5cm wide, 1cm thick ---
void CourtModel::appendLines(ObjMesh& m) {
    float hw = COURT_WIDTH * 0.5f;       // 3.05
    float hl = HALF_LENGTH;               // 6.70
    float sw = SINGLES_WIDTH * 0.5f;     // 2.59
    float sv = SHORT_SERVICE_DIST;       // 1.98
    float ld = hl - DOUBLES_SRV_OFFSET;  // 5.94
    float lw = LINE_WIDTH;               // 2.5cm
    float z  = GROUND_HEIGHT;            // sit on top of ground

    // Doubles sidelines
    addLine3D(m, -hw, -hl, -hw, hl, lw, z);
    addLine3D(m,  hw, -hl,  hw, hl, lw, z);
    // Baselines
    addLine3D(m, -hw,  hl,  hw, hl, lw, z);
    addLine3D(m, -hw, -hl,  hw, -hl, lw, z);
    // Singles sidelines
    addLine3D(m, -sw, -hl, -sw, hl, lw, z);
    addLine3D(m,  sw, -hl,  sw, hl, lw, z);
    // Net line
    addLine3D(m, -hw, 0, hw, 0, lw, z);
    // Short service lines
    addLine3D(m, -hw,  sv, hw,  sv, lw, z);
    addLine3D(m, -hw, -sv, hw, -sv, lw, z);
    // Doubles long service lines
    addLine3D(m, -hw,  ld, hw,  ld, lw, z);
    addLine3D(m, -hw, -ld, hw, -ld, lw, z);
    // Center lines
    addLine3D(m, 0, sv, 0, hl, lw, z);
    addLine3D(m, 0, -hl, 0, -sv, lw, z);

    std::cout << "  Court lines (3D prisms): " << m.vertices.size() << " verts\n";
}
