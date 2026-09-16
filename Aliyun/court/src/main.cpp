#include <iostream>
#include <vector>
#include <cstdio>
#include <glad/glad.h>
#include <GLFW/glfw3.h>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/type_ptr.hpp>

#include "src/shader.h"
#include "src/court_model.h"
#include "src/court_lines.h"
#include "src/net_model.h"
#include "src/obj_exporter.h"

struct CameraState {
    float azimuth = 45.0f, elevation = 30.0f, distance = 18.0f;
    int lastX = 0, lastY = 0; bool dragging = false;
} g_cam;
GLFWwindow* g_window = nullptr;
int g_winW = 1280, g_winH = 720;

struct RenderMesh { GLuint vao=0,vbo=0,ebo=0; int indexCount=0; };
RenderMesh g_groundMesh, g_linesMesh, g_netMesh, g_postsMesh;
ObjMesh g_groundObj, g_linesObj, g_netObj, g_bandObj, g_postsObj;
std::string g_outputDir;

void errorCallback(int,const char* d){std::cerr<<"GLFW err: "<<d<<"\n";}
void mouseButtonCallback(GLFWwindow* w,int b,int a,int){
    if(b==GLFW_MOUSE_BUTTON_LEFT){
        if(a==GLFW_PRESS){g_cam.dragging=true;double x,y;glfwGetCursorPos(w,&x,&y);g_cam.lastX=(int)x;g_cam.lastY=(int)y;}
        else g_cam.dragging=false;
    }
}
void cursorCallback(GLFWwindow* w,double x,double y){
    if(!g_cam.dragging)return;
    int dx=(int)x-g_cam.lastX,dy=(int)y-g_cam.lastY;
    if(glfwGetMouseButton(w,GLFW_MOUSE_BUTTON_LEFT)==GLFW_PRESS){
        g_cam.azimuth+=dx*0.5f;g_cam.elevation=glm::clamp(g_cam.elevation+dy*0.3f,5.0f,85.0f);
    }
    g_cam.lastX=(int)x;g_cam.lastY=(int)y;
}
void scrollCallback(GLFWwindow*,double,double dy){g_cam.distance=glm::clamp(g_cam.distance-(float)dy*1.5f,4.0f,40.0f);}
void keyCallback(GLFWwindow* w,int k,int,int a,int){
    if(a==GLFW_PRESS){if(k==GLFW_KEY_ESCAPE)glfwSetWindowShouldClose(w,true);if(k==GLFW_KEY_R){g_cam.azimuth=45;g_cam.elevation=30;g_cam.distance=18;}}
}
void framebufferSizeCallback(GLFWwindow*,int w,int h){g_winW=w;g_winH=h;glViewport(0,0,w,h);}

void uploadMesh(RenderMesh& rm,const ObjMesh& mesh){
    if(rm.vao){glDeleteVertexArrays(1,&rm.vao);glDeleteBuffers(1,&rm.vbo);glDeleteBuffers(1,&rm.ebo);}
    struct V{float px,py,pz,nx,ny,nz,tu,tv;};
    std::vector<V> v(mesh.vertices.size());
    for(size_t i=0;i<mesh.vertices.size();++i){
        v[i].px=mesh.vertices[i].x;v[i].py=mesh.vertices[i].y;v[i].pz=mesh.vertices[i].z;
        v[i].nx=i<mesh.normals.size()?mesh.normals[i].x:0;
        v[i].ny=i<mesh.normals.size()?mesh.normals[i].y:0;
        v[i].nz=i<mesh.normals.size()?mesh.normals[i].z:1;
        v[i].tu=i<mesh.texcoords.size()?mesh.texcoords[i].x:0;
        v[i].tv=i<mesh.texcoords.size()?mesh.texcoords[i].y:0;
    }
    std::vector<GLuint> idx;
    for(const auto& f:mesh.faces)for(int i=0;i<3;++i)idx.push_back((GLuint)(f.v[i]-1));
    rm.indexCount=(int)idx.size();
    glGenVertexArrays(1,&rm.vao);glGenBuffers(1,&rm.vbo);glGenBuffers(1,&rm.ebo);
    glBindVertexArray(rm.vao);
    glBindBuffer(GL_ARRAY_BUFFER,rm.vbo);glBufferData(GL_ARRAY_BUFFER,v.size()*sizeof(V),v.data(),GL_STATIC_DRAW);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER,rm.ebo);glBufferData(GL_ELEMENT_ARRAY_BUFFER,idx.size()*sizeof(GLuint),idx.data(),GL_STATIC_DRAW);
    glVertexAttribPointer(0,3,GL_FLOAT,GL_FALSE,sizeof(V),(void*)0);glEnableVertexAttribArray(0);
    glVertexAttribPointer(1,3,GL_FLOAT,GL_FALSE,sizeof(V),(void*)(3*sizeof(float)));glEnableVertexAttribArray(1);
    glVertexAttribPointer(2,2,GL_FLOAT,GL_FALSE,sizeof(V),(void*)(6*sizeof(float)));glEnableVertexAttribArray(2);
    glBindVertexArray(0);
}

void generateScene(){
    std::cout<<"\n=== Ground ===\n";
    CourtModel court;
    g_groundObj=court.generateGround();
    std::cout<<"\n=== Court Lines ===\n";
    generateAndExportLines(g_outputDir, g_linesObj);
    std::cout<<"\n=== Net ===\n";
    NetModel net;
    g_netObj=net.generateNet(140,35);
    g_bandObj=net.generateTopBand();
    g_postsObj=net.generateAndExportPosts(g_outputDir, 32);

    std::cout<<"\n=== Uploading ===\n";
    uploadMesh(g_groundMesh,g_groundObj);
    uploadMesh(g_linesMesh,g_linesObj);

    ObjMesh nc=g_netObj;
    int vo=(int)nc.vertices.size(),vto=(int)nc.texcoords.size(),vno=(int)nc.normals.size();
    nc.vertices.insert(nc.vertices.end(),g_bandObj.vertices.begin(),g_bandObj.vertices.end());
    nc.texcoords.insert(nc.texcoords.end(),g_bandObj.texcoords.begin(),g_bandObj.texcoords.end());
    nc.normals.insert(nc.normals.end(),g_bandObj.normals.begin(),g_bandObj.normals.end());
    for(const auto& f:g_bandObj.faces){ObjMesh::Face nf=f;for(int i=0;i<3;++i){nf.v[i]+=vo;nf.vt[i]+=vto;nf.vn[i]+=vno;}nc.faces.push_back(nf);}
    uploadMesh(g_netMesh,nc);
    uploadMesh(g_postsMesh,g_postsObj);
}

void exportScene(){
    std::cout<<"\n=== Exporting ===\n";
    ObjExporter::write(g_outputDir+"\\court_ground.obj",g_groundObj,"court.mtl");
    ObjExporter::writeMtl(g_outputDir+"\\court.mtl",glm::vec3(0.18,0.55,0.33));
    ObjExporter::write(g_outputDir+"\\net.obj",g_netObj,"net.mtl");
    ObjExporter::writeMtl(g_outputDir+"\\net.mtl",glm::vec3(0.9,0.9,0.9),0.7);
    ObjExporter::write(g_outputDir+"\\net_band.obj",g_bandObj,"band.mtl");
    ObjExporter::writeMtl(g_outputDir+"\\band.mtl",glm::vec3(1,1,1));
    std::vector<ObjExporter::Group> all={{"ground",g_groundObj},{"lines",g_linesObj},{"net",g_netObj},{"band",g_bandObj},{"posts",g_postsObj}};
    ObjExporter::writeGroups(g_outputDir+"\\badminton_court_complete.obj",all,"court.mtl");
    std::cout<<"Exported to "<<g_outputDir<<"\n";
}

void render(Shader& lit,Shader& unlit){
    glClearColor(0.08f,0.08f,0.12f,1);glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT);
    float ar=glm::radians(g_cam.azimuth),er=glm::radians(g_cam.elevation);
    glm::mat4 view=glm::lookAt(glm::vec3(g_cam.distance*cos(er)*sin(ar),g_cam.distance*cos(er)*cos(ar),g_cam.distance*sin(er)),glm::vec3(0,0,0.8f),glm::vec3(0,0,1));
    glm::mat4 proj=glm::perspective(glm::radians(45.0f),(float)g_winW/(float)g_winH,0.1f,80.0f);
    glm::mat4 model(1);

    auto du=[&](RenderMesh& rm,glm::vec3 c){
        unlit.use();unlit.setMat4("view",view);unlit.setMat4("proj",proj);unlit.setMat4("model",model);
        unlit.setVec3("color",c);glBindVertexArray(rm.vao);glDrawElements(GL_TRIANGLES,rm.indexCount,GL_UNSIGNED_INT,0);
    };
    auto dl=[&](RenderMesh& rm,glm::vec3 c,float a){
        lit.use();lit.setMat4("view",view);lit.setMat4("proj",proj);lit.setMat4("model",model);
        lit.setVec3("color",c);lit.setVec3("lightDir",glm::normalize(glm::vec3(0.5,-0.5,0.8)));lit.setFloat("ambient",a);
        glBindVertexArray(rm.vao);glDrawElements(GL_TRIANGLES,rm.indexCount,GL_UNSIGNED_INT,0);
    };

    du(g_groundMesh,glm::vec3(0.18f,0.55f,0.33f));  // green ground (unlit, uniform color)
    du(g_linesMesh,glm::vec3(1,1,1));               // white lines (unlit)
    dl(g_netMesh,glm::vec3(0.85,0.85,0.85),0.55f);         // net (lit)
    dl(g_postsMesh,glm::vec3(1.0,0.85,0.1),0.45f);         // posts (lit)
    glBindVertexArray(0);
}

int main(int argc,char** argv){
    g_outputDir=std::string(argv[0]);
    auto p=g_outputDir.find_last_of("\\/");
    if(p!=std::string::npos)g_outputDir=g_outputDir.substr(0,p);
    g_outputDir+="\\output";
    std::cout<<"===== BWF Court 3D =====\nOutput: "<<g_outputDir<<"\n";

    glfwSetErrorCallback(errorCallback);
    if(!glfwInit())return-1;
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR,3);glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR,3);
    glfwWindowHint(GLFW_OPENGL_PROFILE,GLFW_OPENGL_CORE_PROFILE);
    g_window=glfwCreateWindow(g_winW,g_winH,"BWF Court | Drag=rotate R=reset",nullptr,nullptr);
    if(!g_window){glfwTerminate();return-1;}
    glfwMakeContextCurrent(g_window);
    glfwSetMouseButtonCallback(g_window,mouseButtonCallback);
    glfwSetCursorPosCallback(g_window,cursorCallback);
    glfwSetScrollCallback(g_window,scrollCallback);
    glfwSetKeyCallback(g_window,keyCallback);
    glfwSetFramebufferSizeCallback(g_window,framebufferSizeCallback);
    if(!gladLoadGLLoader((GLADloadproc)glfwGetProcAddress))return-1;
    glEnable(GL_DEPTH_TEST);

    const char* vs=R"(#version 330 core
layout(location=0)in vec3 aPos;layout(location=1)in vec3 aNormal;layout(location=2)in vec2 aTexCoord;
uniform mat4 model,view,proj;out vec3 fn;
void main(){gl_Position=proj*view*model*vec4(aPos,1);fn=mat3(transpose(inverse(model)))*aNormal;})";
    const char* litFs=R"(#version 330 core
in vec3 fn;uniform vec3 color,lightDir;uniform float ambient;out vec4 FC;
void main(){vec3 n=normalize(fn);if(!gl_FrontFacing)n=-n;
float d=max(0,dot(n,normalize(lightDir)));FC=vec4(color*(ambient+(1-ambient)*d),1);})";
    const char* fs=R"(#version 330 core
uniform vec3 color;out vec4 FC;void main(){FC=vec4(color,1);})";

    Shader mainShader,unlitShader;
    mainShader.loadFromStrings(vs,litFs);unlitShader.loadFromStrings(vs,fs);

    generateScene();exportScene();
    std::cout<<"\n[Drag] rotate [Scroll] zoom [R] reset [ESC] quit\n";
    while(!glfwWindowShouldClose(g_window)){render(mainShader,unlitShader);glfwSwapBuffers(g_window);glfwPollEvents();}
    glfwTerminate();
    return 0;
}
