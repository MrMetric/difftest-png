CXXFLAGS ?= -O2 -Wall -Wextra
shim: shim.cpp lodepng.cpp lodepng.h
	$(CXX) $(CXXFLAGS) -o $@ shim.cpp lodepng.cpp
clean:
	rm -f shim
