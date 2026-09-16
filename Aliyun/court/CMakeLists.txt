cmake_minimum_required(VERSION 3.10)
project(CourtModelGenerator VERSION 1.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Source files
set(SOURCES
    src/main.cpp
    src/court_model.cpp
    src/court_lines.cpp
    src/net_model.cpp
    src/shader.cpp
    src/obj_exporter.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/glad/glad.c
)

# Include directories
include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/glad
    ${CMAKE_CURRENT_SOURCE_DIR}/GLFW
    ${CMAKE_CURRENT_SOURCE_DIR}/KHR
    ${CMAKE_CURRENT_SOURCE_DIR}/glm
)

# Output directory for generated OBJ files
file(MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/output)

# Create executable
add_executable(court_generator ${SOURCES})

# Link libraries (MSVC-compatible GLFW static lib)
target_link_libraries(court_generator
    ${CMAKE_CURRENT_SOURCE_DIR}/glfw_temp/glfw-3.4.bin.WIN64/lib-vc2022/glfw3_mt.lib
    opengl32
    gdi32
)

# Create output directory in build folder
if(WIN32)
    add_custom_command(TARGET court_generator POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory
            "$<TARGET_FILE_DIR:court_generator>/output"
    )
endif()
