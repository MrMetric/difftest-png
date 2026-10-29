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
# The library returned without error but not an image the shim can write: its result, exit 71.
class ApiError < StandardError; end
begin
  raise ApiError, "invalid image dimensions #{img.width}x#{img.height}" unless img.width > 0 && img.height > 0
  raise ApiError, "#{img.pixels.length} pixels for #{img.width}x#{img.height}" unless img.pixels.length == img.width * img.height
  File.open(ARGV[1], 'wb') do |f|
    f.write "P7\nWIDTH #{img.width}\nHEIGHT #{img.height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    img.pixels.each_slice(img.width) do |row|
      bad = row.index { |p| !p.is_a?(Integer) || p < 0 || p > 0xFFFFFFFF }
      raise ApiError, "pixel value #{row[bad].inspect} is not a 32-bit RGBA integer" unless bad.nil?
      begin
        f.write row.pack('N*')
      rescue TypeError, RangeError => e
        raise ApiError, "pixel values: #{e.message}"
      end
    end
  end
rescue ApiError => e
  puts "API #{e.message}"
  exit 71
rescue Exception => e
  puts "SHIM #{e.class}: #{e.message}"
  exit 70
end
puts "OK #{img.width} #{img.height} 4 8"
