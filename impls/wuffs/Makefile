CXXFLAGS ?= -O2 -Wall -Wextra -Wno-unused-parameter
shim: shim.cpp wuffs-v0.4.c
	$(CXX) $(CXXFLAGS) -o $@ shim.cpp
clean:
	rm -f shim
