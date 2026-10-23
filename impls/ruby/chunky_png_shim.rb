#!/usr/bin/env ruby
# ChunkyPNG shim. Policy: Image.from_file, pixels written as RGBA8 (ChunkyPNG
# truncates 16-bit samples to 8 bits, so the column is max_depth 8).
begin
  require 'chunky_png'
rescue LoadError => e # 127: the decoder library is not installed (see impls/CONTRACT.md)
  puts "ERR decoder library not installed: #{e.message}"
  exit 127
end

if ARGV == ['--version']
  puts "chunky_png #{ChunkyPNG::VERSION} (ruby #{RUBY_VERSION})"
  exit 0
end
begin
  img = ChunkyPNG::Image.from_file(ARGV[0])
rescue Exception => e
  puts "ERR #{e.class}: #{e.message}"
  exit 1
end
File.open(ARGV[1], 'wb') do |f|
  f.write "P7\nWIDTH #{img.width}\nHEIGHT #{img.height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
  f.write img.pixels.pack('N*')
end
puts "OK #{img.width} #{img.height} 4 8"
