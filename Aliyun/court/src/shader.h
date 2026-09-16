#ifndef SHADER_H
#define SHADER_H

#include <string>
#include <glad/glad.h>
#include <glm/glm.hpp>

class Shader {
public:
    Shader() : program(0) {}
    ~Shader();

    bool loadFromStrings(const std::string& vertexSource,
                         const std::string& fragmentSource);
    void use() const;
    void setMat4(const std::string& name, const glm::mat4& mat) const;
    void setVec3(const std::string& name, const glm::vec3& vec) const;
    void setFloat(const std::string& name, float value) const;
    void setInt(const std::string& name, int value) const;

private:
    GLuint program;
    GLuint compileShader(GLenum type, const std::string& source);
};

#endif
