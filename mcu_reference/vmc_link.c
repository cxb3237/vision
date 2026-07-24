#include "vmc_link.h"

#include <string.h>

static uint16_t read_u16_le(const uint8_t *data) {
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static int16_t read_i16_le(const uint8_t *data) {
    return (int16_t)read_u16_le(data);
}

static uint32_t read_u32_le(const uint8_t *data) {
    return (uint32_t)data[0]
        | ((uint32_t)data[1] << 8)
        | ((uint32_t)data[2] << 16)
        | ((uint32_t)data[3] << 24);
}

void vmc_link_parser_init(vmc_link_parser_t *parser) {
    if (parser != NULL) {
        memset(parser, 0, sizeof(*parser));
    }
}

uint16_t vmc_link_crc16_ccitt_false(const uint8_t *data, size_t length) {
    uint16_t crc = 0xFFFFu;
    size_t index;
    uint8_t bit;
    for (index = 0u; index < length; ++index) {
        crc ^= (uint16_t)data[index] << 8;
        for (bit = 0u; bit < 8u; ++bit) {
            if ((crc & 0x8000u) != 0u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

static bool header_valid(const uint8_t *data) {
    return data[0] == VMC_LINK_SOF1
        && data[1] == VMC_LINK_SOF2
        && data[2] == VMC_LINK_VERSION
        && data[3] == VMC_LINK_RESULT_TYPE
        && data[4] == VMC_LINK_PAYLOAD_LENGTH;
}

static void resynchronize(vmc_link_parser_t *parser) {
    uint8_t position;
    for (position = 1u; position + 1u < parser->length; ++position) {
        if (parser->buffer[position] == VMC_LINK_SOF1
                && parser->buffer[position + 1u] == VMC_LINK_SOF2) {
            uint8_t remaining = (uint8_t)(parser->length - position);
            memmove(parser->buffer, &parser->buffer[position], remaining);
            parser->length = remaining;
            return;
        }
    }
    if (parser->buffer[parser->length - 1u] == VMC_LINK_SOF1) {
        parser->buffer[0] = VMC_LINK_SOF1;
        parser->length = 1u;
    } else {
        parser->length = 0u;
    }
}

static void decode_result(const uint8_t *data, vmc_link_result_t *out) {
    out->sequence = read_u16_le(&data[5]);
    out->timestamp_ms = read_u32_le(&data[7]);
    out->detector_id = data[11];
    out->state = data[12];
    out->target_class = read_u16_le(&data[13]);
    out->center_x_px = read_i16_le(&data[15]);
    out->center_y_px = read_i16_le(&data[17]);
    out->error_x_permille = read_i16_le(&data[19]);
    out->error_y_permille = read_i16_le(&data[21]);
    out->bbox_width_px = read_u16_le(&data[23]);
    out->bbox_height_px = read_u16_le(&data[25]);
    out->confidence_permille = read_u16_le(&data[27]);
    out->distance_mm = read_u16_le(&data[29]);
    out->flags = data[31];
}

bool vmc_link_feed_byte(
    vmc_link_parser_t *parser,
    uint8_t byte,
    vmc_link_result_t *out
) {
    uint16_t received_crc;
    uint16_t calculated_crc;
    if (parser == NULL || out == NULL) {
        return false;
    }
    if (parser->length == 0u) {
        if (byte == VMC_LINK_SOF1) {
            parser->buffer[0] = byte;
            parser->length = 1u;
        }
        return false;
    }
    if (parser->length == 1u) {
        if (byte == VMC_LINK_SOF2) {
            parser->buffer[1] = byte;
            parser->length = 2u;
        } else if (byte != VMC_LINK_SOF1) {
            parser->length = 0u;
        }
        return false;
    }
    parser->buffer[parser->length++] = byte;
    if (parser->length == 5u && !header_valid(parser->buffer)) {
        parser->header_error_count++;
        resynchronize(parser);
        return false;
    }
    if (parser->length < VMC_LINK_PACKET_SIZE) {
        return false;
    }
    received_crc = read_u16_le(&parser->buffer[32]);
    calculated_crc = vmc_link_crc16_ccitt_false(&parser->buffer[2], 30u);
    if (received_crc != calculated_crc) {
        parser->crc_error_count++;
        resynchronize(parser);
        return false;
    }
    decode_result(parser->buffer, out);
    parser->good_count++;
    parser->length = 0u;
    return true;
}
