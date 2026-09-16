#ifndef OBJ_EXPORTER_H
#define OBJ_EXPORTER_H

#include <string>
#include <vector>
#include <glm/glm.hpp>

struct ObjMesh {
    std::vector<glm::vec3> vertices;
    std::vector<glm::vec3> normals;
    std::vector<glm::vec2> texcoords;
    // Face: each element is 3 indices (vertex, texcoord, normal)
    struct Face {
        int v[3], vt[3], vn[3];
    };
    std::vector<Face> faces;

    void clear() {
        vertices.clear(); normals.clear(); texcoords.clear(); faces.clear();
    }
};

class ObjExporter {
public:
    // Write a single mesh as one OBJ file
    static bool write(const std::string& filepath, const ObjMesh& mesh,
                      const std::string& mtlName = "");

    // Write a group of meshes into one OBJ file with groups
    struct Group {
        std::string name;
        ObjMesh mesh;
    };
    static bool writeGroups(const std::string& filepath,
                            const std::vector<Group>& groups,
                            const std::string& mtlName = "");

    // Write a simple MTL file
    static bool writeMtl(const std::string& filepath,
                         const glm::vec3& diffuse,
                         float opacity = 1.0f,
                         const std::string& textureName = "");
};

#endif
