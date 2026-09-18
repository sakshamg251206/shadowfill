cmake_minimum_required(VERSION 3.24)
project(shadowfill_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Explicit options rather than filesystem probing: a missing or mistyped source
# path must fail CMake loudly, never quietly produce a wheel with no extension
# module in it, which would let the equivalence tests skip themselves green.
option(SHADOWFILL_BUILD_CORE "Build the C++ engine" ON)
option(SHADOWFILL_BUILD_TESTS "Build C++ tests" OFF)
# `make test-cpp` wants only the Catch2 binary and has no pybind11 in scope;
# the wheel build wants only the module. find_package stays REQUIRED either way.
option(SHADOWFILL_BUILD_BINDINGS "Build the pybind11 module" ON)

if(SHADOWFILL_BUILD_CORE)
    add_library(shadowfill_core
        src/shadowfill_core/book.cpp
        src/shadowfill_core/shadow.cpp
    )
    target_include_directories(shadowfill_core PUBLIC src/shadowfill_core/include)
    target_compile_options(shadowfill_core PRIVATE -O3 -Wall -Wextra -Wpedantic -Werror)

    if(SHADOWFILL_BUILD_TESTS)
        include(FetchContent)
        FetchContent_Declare(Catch2
            GIT_REPOSITORY https://github.com/catchorg/Catch2.git
            GIT_TAG v3.6.0)
        FetchContent_MakeAvailable(Catch2)
        add_executable(cpp_tests tests/cpp/test_book.cpp tests/cpp/test_shadow.cpp)
        target_link_libraries(cpp_tests PRIVATE shadowfill_core Catch2::Catch2WithMain)
        enable_testing()
        add_test(NAME cpp_tests COMMAND cpp_tests)
    endif()

    if(SHADOWFILL_BUILD_BINDINGS)
        find_package(pybind11 CONFIG REQUIRED)
        pybind11_add_module(_core src/shadowfill_bindings/module.cpp)
        target_link_libraries(_core PRIVATE shadowfill_core)
        install(TARGETS _core DESTINATION shadowfill)
    endif()
endif()
