# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
cmake_minimum_required(VERSION 3.24)
project(ThoughtsClient VERSION 0.3.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.6 REQUIRED COMPONENTS Core Network)

add_library(thoughtsclient STATIC
    include/thoughts/durableoutbox.h
    include/thoughts/syncclient.h
    src/durableoutbox.cpp
    src/syncclient.cpp
)

target_include_directories(thoughtsclient PUBLIC include)
target_link_libraries(thoughtsclient PUBLIC Qt6::Core Qt6::Network)
target_compile_definitions(thoughtsclient PRIVATE QT_NO_CAST_FROM_ASCII QT_NO_CAST_TO_ASCII)

include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 6.6 REQUIRED COMPONENTS Test)
    add_executable(thoughtsclienttest tests/clienttest.cpp)
    target_link_libraries(thoughtsclienttest PRIVATE thoughtsclient Qt6::Test)
    target_compile_definitions(thoughtsclienttest PRIVATE THOUGHTS_PROTOCOL_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/../../protocol/fixtures")
    add_test(NAME thoughtsclienttest COMMAND thoughtsclienttest)
    add_executable(commentservicetest tests/commentservicetest.cpp)
    target_link_libraries(commentservicetest PRIVATE thoughtsclient Qt6::Test)
    target_compile_definitions(commentservicetest PRIVATE THOUGHTS_PROTOCOL_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/../../protocol/fixtures")
    add_test(NAME commentservicetest COMMAND commentservicetest)
endif()
