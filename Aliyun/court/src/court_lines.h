#ifndef COURT_LINES_H
#define COURT_LINES_H

#include "obj_exporter.h"
#include <string>

/**
 * Generate all 13 BWF court lines and export each as individual .obj file
 *
 * @param outputDir     export directory (no trailing backslash)
 * @param combinedLines [out] merged mesh of all lines for OpenGL rendering
 * @return              number of lines exported (13)
 */
int generateAndExportLines(const std::string& outputDir, ObjMesh& combinedLines);

#endif
