//! Rust `png` crate shim: decode one PNG with the crate's defaults and emit PAM.
//!
//! Policy: `Transformations::EXPAND` only (palette -> RGB(A), tRNS -> alpha,
//! low bit depths -> 8). No 16->8 stripping, no gamma. CRC/Adler policy and
//! memory limits are the crate's defaults, measured rather than configured.
//! Rows are read one at a time (`Reader::next_interlaced_row`) and written out
//! as they arrive, with an Adam7 merge row but never another image. The crate
//! names a row's Adam7 pass only through a type with private fields, so the
//! shim walks the pass sequence itself and checks each row's length against
//! it. After the rows nothing more is read, as in the crate's documented use;
//! 16-bit samples are already big-endian in the crate's output.
use std::fs::File;
use std::io::{BufReader, Write};
use std::os::unix::fs::FileExt;

/// PNG 8.2 Adam7: starting column, starting row, column step, row step of passes 0-6.
const ADAM7: [[u64; 4]; 7] = [[0, 0, 8, 8], [4, 0, 8, 8], [0, 4, 4, 8], [2, 0, 4, 4], [0, 2, 2, 4], [1, 0, 2, 2], [0, 1, 1, 2]];

fn pass_cols(width: u64, pass: usize) -> u64 {
	let [start, _, step, _] = ADAM7[pass];
	width.saturating_sub(start).div_ceil(step)
}

fn pass_rows(height: u64, pass: usize) -> u64 {
	let [_, start, _, step] = ADAM7[pass];
	height.saturating_sub(start).div_ceil(step)
}

fn fail(msg: &str) -> ! {
	println!("ERR {msg}");
	std::process::exit(1);
}

fn shim_fail(msg: &str) -> ! {
	println!("SHIM {msg}");
	std::process::exit(70);
}

/// The crate returned without error but not an image the shim can write: its result.
fn api_fail(msg: &str) -> ! {
	println!("API {msg}");
	std::process::exit(71);
}

/// The output row at `off`; bytes the file does not reach yet read as zero.
fn read_row_at(f: &File, buf: &mut [u8], mut off: u64) {
	let mut got = 0;
	while got < buf.len() {
		match f.read_at(&mut buf[got..], off) {
			Ok(0) => {
				buf[got..].fill(0);
				return;
			}
			Ok(n) => {
				got += n;
				off += n as u64;
			}
			Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
			Err(e) => shim_fail(&format!("read output: {e}")),
		}
	}
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() != 3 {
		eprintln!("usage: rust-png-shim in.png out.pam");
		std::process::exit(2);
	}
	let f = File::open(&args[1]).unwrap_or_else(|e| shim_fail(&format!("open: {e}")));
	let mut dec = png::Decoder::new(BufReader::new(f));
	dec.set_transformations(png::Transformations::EXPAND);
	let mut reader = dec.read_info().unwrap_or_else(|e| fail(&e.to_string()));
	let (w, h, interlaced) = {
		let info = reader.info();
		println!(
			"INFO ihdr width={} height={} bit_depth={} color_type={} interlaced={}",
			info.width, info.height, info.bit_depth as u8, info.color_type as u8, info.interlaced
		);
		let mut chunks: Vec<&str> = Vec::new();
		if info.palette.is_some() { chunks.push("PLTE"); }
		if info.trns.is_some() { chunks.push("tRNS"); }
		if info.gama_chunk.is_some() { chunks.push("gAMA"); }
		if info.chrm_chunk.is_some() { chunks.push("cHRM"); }
		if info.srgb.is_some() { chunks.push("sRGB"); }
		if info.icc_profile.is_some() { chunks.push("iCCP"); }
		if info.sbit.is_some() { chunks.push("sBIT"); }
		if info.pixel_dims.is_some() { chunks.push("pHYs"); }
		if info.animation_control.is_some() { chunks.push("acTL"); }
		println!(
			"INFO chunks={} text={}",
			chunks.join(","),
			info.uncompressed_latin1_text.len() + info.compressed_latin1_text.len() + info.utf8_text.len()
		);
		(info.width as u64, info.height as u64, info.interlaced)
	};
	let (color_type, bit_depth) = reader.output_color_type();
	let channels = color_type.samples();
	let depth = bit_depth as u8;
	let maxval = match depth {
		8 => 255,
		16 => 65535,
		d => api_fail(&format!("unexpected output bit depth {d}")),
	};
	let tt = match channels {
		1 => "GRAYSCALE",
		2 => "GRAYSCALE_ALPHA",
		3 => "RGB",
		4 => "RGB_ALPHA",
		n => api_fail(&format!("unexpected channel count {n}")),
	};
	let bpp = channels * (depth as usize / 8);
	let rowbytes = reader.output_line_size(w as u32).unwrap_or_else(|| shim_fail("row size overflow"));
	if rowbytes != w as usize * bpp {
		api_fail(&format!("row size {rowbytes} for {w} pixels of {bpp} bytes"));
	}
	let out = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(true).open(&args[2])
		.unwrap_or_else(|e| shim_fail(&format!("create: {e}")));
	let header = format!("P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n");
	(&out).write_all(header.as_bytes()).unwrap_or_else(|e| shim_fail(&format!("write: {e}")));
	let body = header.len() as u64;
	let write_at = |data: &[u8], off: u64| out.write_all_at(data, off).unwrap_or_else(|e| shim_fail(&format!("write: {e}")));
	if !interlaced {
		let mut y: u64 = 0;
		while let Some(row) = reader.next_interlaced_row().unwrap_or_else(|e| fail(&e.to_string())) {
			let data = row.data();
			if data.len() != rowbytes || y >= h {
				api_fail(&format!("row {y} of {h} holds {} bytes, not {rowbytes}", data.len()));
			}
			write_at(data, body + y * rowbytes as u64);
			y += 1;
		}
		if y != h {
			api_fail(&format!("{y} rows decoded of {h}"));
		}
	} else {
		let mut passes = (0..7)
			.filter(|&p| pass_cols(w, p) > 0)
			.flat_map(|p| (0..pass_rows(h, p)).map(move |line| (p, line)));
		let mut scratch = vec![0u8; rowbytes];
		while let Some(row) = reader.next_interlaced_row().unwrap_or_else(|e| fail(&e.to_string())) {
			let (p, line) = passes.next().unwrap_or_else(|| api_fail("more rows than Adam7 defines"));
			let n = pass_cols(w, p) as usize;
			let data = row.data();
			if data.len() != n * bpp {
				api_fail(&format!("pass {} row {line} holds {} bytes, not {}", p + 1, data.len(), n * bpp));
			}
			let [xs, ys, xstep, ystep] = ADAM7[p];
			let off = body + (ys + line * ystep) * rowbytes as u64;
			read_row_at(&out, &mut scratch, off);
			for i in 0..n {
				let dst = (xs as usize + i * xstep as usize) * bpp;
				scratch[dst..dst + bpp].copy_from_slice(&data[i * bpp..(i + 1) * bpp]);
			}
			write_at(&scratch, off);
		}
		if passes.next().is_some() {
			api_fail("fewer rows than Adam7 defines");
		}
	}
	let len = out.metadata().map(|m| m.len()).unwrap_or_else(|e| shim_fail(&format!("stat output: {e}")));
	if len != body + h * rowbytes as u64 {
		shim_fail("output incomplete");
	}
	println!("OK {w} {h} {channels} {depth}");
}
