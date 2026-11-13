// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
#include <synctex_parser.h>
#include <zip.h>
#include <zlib.h>

#include <algorithm>
#include <charconv>
#include <cstdio>
#include <cmath>
#include <cstdlib>
#include <map>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

struct Request {
    std::string id;
    std::string source;
    int first_line = 0;
    int last_line = 0;
};

bool parse_integer(std::string_view value, int &result) {
    const auto *first = value.data();
    const auto *last = first + value.size();
    const auto parsed = std::from_chars(first, last, result);
    return parsed.ec == std::errc{} && parsed.ptr == last;
}

bool parse_request(const std::string &line, Request &request) {
    std::vector<std::string_view> fields;
    std::size_t start = 0;
    while (fields.size() < 3) {
        const auto separator = line.find('\t', start);
        if (separator == std::string::npos) {
            return false;
        }
        fields.emplace_back(line.data() + start, separator - start);
        start = separator + 1;
    }
    fields.emplace_back(line.data() + start, line.size() - start);
    if (fields.size() != 4 || fields[0].empty() || fields[1].empty()) {
        return false;
    }
    request.id.assign(fields[0]);
    request.source.assign(fields[1]);
    return parse_integer(fields[2], request.first_line)
        && parse_integer(fields[3], request.last_line)
        && request.first_line > 0
        && request.last_line >= request.first_line;
}

void emit_node(const Request &request, synctex_node_p node) {
    const int page = synctex_node_page(node);
    const float left = synctex_node_box_visible_h(node);
    const float baseline = synctex_node_box_visible_v(node);
    const float width = synctex_node_box_visible_width(node);
    const float height = synctex_node_box_visible_height(node);
    const float depth = synctex_node_box_visible_depth(node);
    if (page < 1 || !std::isfinite(left) || !std::isfinite(baseline)
        || !std::isfinite(width) || !std::isfinite(height) || !std::isfinite(depth)) {
        return;
    }
    std::cout << request.id << '\t' << page << '\t' << left << '\t'
              << baseline - height - 2.0F << '\t' << left + width << '\t'
              << baseline + depth + 2.0F << '\n';
}

// contents must stay alive until zip_close: libzip does not copy the buffer.
bool add_zip_entry(zip_t *archive, const std::string &name, const std::string &contents) {
    zip_source_t *source = zip_source_buffer(archive, contents.data(), contents.size(), 0);
    if (source == nullptr) {
        return false;
    }
    const zip_int64_t index = zip_file_add(archive, name.c_str(), source, ZIP_FL_OVERWRITE);
    if (index < 0) {
        zip_source_free(source);
        return false;
    }
    return zip_set_file_compression(archive, index, ZIP_CM_DEFLATE, 9) == 0
        && zip_file_set_mtime(archive, index, 315532800, 0) == 0;
}

int reverse_index(const char *synctex_path, const char *output_path) {
    std::unordered_set<std::string> authored;
    std::vector<Request> queries;
    std::string input;
    while (std::getline(std::cin, input)) {
        if (input.starts_with("A\t")) {
            authored.insert(input.substr(2));
        } else if (input.starts_with("Q\t")) {
            Request request;
            if (!parse_request(input.substr(2), request)) {
                std::cerr << "invalid analysis query\n";
                return 65;
            }
            queries.push_back(std::move(request));
        } else if (!input.empty()) {
            authored.insert(input);
        }
    }
    gzFile stream = gzopen(synctex_path, "rb");
    if (stream == nullptr) {
        std::cerr << "could not open " << synctex_path << '\n';
        return 65;
    }
    std::unordered_map<int, std::string> tags;
    std::map<int, std::string> pages;
    double unit = 1.0;
    double magnification = 1000.0;
    double x_offset = 0.0;
    double y_offset = 0.0;
    int current_page = 0;
    char buffer[65536];
    while (gzgets(stream, buffer, sizeof(buffer)) != nullptr) {
        std::string line(buffer);
        while (!line.empty() && line.back() != '\n' && !gzeof(stream)
               && gzgets(stream, buffer, sizeof(buffer)) != nullptr) {
            line.append(buffer);
        }
        while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) {
            line.pop_back();
        }
        if (line.starts_with("Input:")) {
            const auto separator = line.find(':', 6);
            int tag = 0;
            if (separator != std::string::npos
                && parse_integer(std::string_view(line).substr(6, separator - 6), tag)) {
                tags[tag] = line.substr(separator + 1);
            }
            continue;
        }
        const auto parse_header = [&line](std::string_view prefix, double &value) {
            if (!line.starts_with(prefix)) {
                return false;
            }
            char *end = nullptr;
            value = std::strtod(line.c_str() + prefix.size(), &end);
            return end != line.c_str() + prefix.size();
        };
        if (parse_header("Unit:", unit) || parse_header("Magnification:", magnification)
            || parse_header("X Offset:", x_offset) || parse_header("Y Offset:", y_offset)) {
            continue;
        }
        if (line.size() > 1 && line.front() == '{') {
            parse_integer(std::string_view(line).substr(1), current_page);
            continue;
        }
        if (current_page < 1 || line.empty()
            || std::string_view("[(hvrx").find(line.front()) == std::string_view::npos) {
            continue;
        }
        int tag = 0;
        int source_line = 0;
        int h = 0;
        int v = 0;
        int width = 0;
        int height = 0;
        int depth = 0;
        const int matched = std::sscanf(
            line.c_str() + 1,
            "%d,%d:%d,%d:%d,%d,%d",
            &tag,
            &source_line,
            &h,
            &v,
            &width,
            &height,
            &depth
        );
        const auto source = tags.find(tag);
        if (matched < 4 || source_line < 1 || source == tags.end()) {
            continue;
        }
        const double scale = unit * magnification / 1000.0 / 65781.76;
        const double x = static_cast<double>(h) * scale + x_offset * scale;
        const double y = static_cast<double>(v) * scale + y_offset * scale;
        char record[2048];
        const int length = std::snprintf(
            record,
            sizeof(record),
            "%c\t%s\t%d\t%.8g\t%.8g\t%.8g\t%.8g\t%.8g\n",
            line.front(),
            source->second.c_str(),
            source_line,
            x,
            y,
            static_cast<double>(width) * scale,
            static_cast<double>(height) * scale,
            static_cast<double>(depth) * scale
        );
        if (length < 0 || static_cast<std::size_t>(length) >= sizeof(record)) {
            gzclose(stream);
            std::cerr << "SyncTeX record is too long\n";
            return 65;
        }
        if (authored.contains(source->second)) {
            pages[current_page].append(record, static_cast<std::size_t>(length));
        }
        // Compact SyncTeX records such as `x` nodes have only a source point.
        // Preserve those points for the Python resolver, which clusters them into
        // line bands instead of treating exact point equality as a glyph box.
        for (const Request &request : queries) {
            if (request.source == source->second && source_line >= request.first_line
                && source_line <= request.last_line) {
                const double node_width = static_cast<double>(width) * scale;
                const double node_height = static_cast<double>(height) * scale;
                const double node_depth = static_cast<double>(depth) * scale;
                const double left = std::min(x, x + node_width);
                const double right = std::max(x, x + node_width);
                const double top = std::min(y - node_height - 2.0, y + node_depth + 2.0);
                const double bottom = std::max(y - node_height - 2.0, y + node_depth + 2.0);
                std::cout << request.id << '\t' << current_page << '\t' << left << '\t'
                          << top << '\t' << right << '\t' << bottom << '\n';
            }
        }
    }
    if (gzclose(stream) != Z_OK || pages.empty()) {
        std::cerr << "SyncTeX data has no authored nodes\n";
        return 65;
    }
    int error = 0;
    zip_t *archive = zip_open(output_path, ZIP_CREATE | ZIP_TRUNCATE, &error);
    if (archive == nullptr) {
        std::cerr << "could not create reverse SyncTeX archive\n";
        return 74;
    }
    // libzip reads entry buffers only in zip_close, so each must outlive it.
    static const std::string format = "{\"schema_version\":1,\"format\":\"thoughts-reverse-synctex\"}\n";
    bool valid = add_zip_entry(archive, "format.json", format);
    for (const auto &[page, contents] : pages) {
        valid = valid && add_zip_entry(archive, "pages/" + std::to_string(page) + ".tsv", contents);
    }
    if (!valid || zip_close(archive) != 0) {
        if (!valid) {
            zip_discard(archive);
        }
        std::cerr << "could not finalize reverse SyncTeX archive\n";
        return 74;
    }
    return EXIT_SUCCESS;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc == 4
        && (std::string_view(argv[1]) == "reverse-index"
            || std::string_view(argv[1]) == "analyze")) {
        return reverse_index(argv[2], argv[3]);
    }
    if (argc != 3 || std::string_view(argv[1]) != "synctex-boxes") {
        std::cerr << "usage: thoughts-source-artifact-worker synctex-boxes PDF\n"
                     "       thoughts-source-artifact-worker reverse-index SYNCTEX OUTPUT\n";
        return 64;
    }
    synctex_scanner_p scanner = synctex_scanner_new_with_output_file(argv[2], nullptr, 1);
    if (scanner == nullptr) {
        std::cerr << "could not parse SyncTeX data for " << argv[2] << '\n';
        return 65;
    }
    std::string line;
    while (std::getline(std::cin, line)) {
        Request request;
        if (!parse_request(line, request)) {
            synctex_scanner_free(scanner);
            std::cerr << "invalid request line\n";
            return 65;
        }
        for (int source_line = request.first_line; source_line <= request.last_line; ++source_line) {
            if (synctex_display_query(scanner, request.source.c_str(), source_line, 1, 0) <= 0) {
                continue;
            }
            while (synctex_node_p node = synctex_scanner_next_result(scanner)) {
                emit_node(request, node);
            }
        }
    }
    synctex_scanner_free(scanner);
    return std::cout.good() ? EXIT_SUCCESS : EXIT_FAILURE;
}
