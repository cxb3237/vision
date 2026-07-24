#ifndef VMC_LINK_H
#define VMC_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define VMC_LINK_PACKET_SIZE 34u
#define VMC_LINK_SOF1 0xAAu
#define VMC_LINK_SOF2 0x55u
#define VMC_LINK_VERSION 1u
#define VMC_LINK_RESULT_TYPE 0x01u
#define VMC_LINK_PAYLOAD_LENGTH 27u

typedef struct {
    uint16_t sequence;
    uint32_t timestamp_ms;
    uint8_t detector_id;
    uint8_t state;
    uint16_t target_class;
    int16_t center_x_px;
    int16_t center_y_px;
    int16_t error_x_permille;
    int16_t error_y_permille;
    uint16_t bbox_width_px;
    uint16_t bbox_height_px;
    uint16_t confidence_permille;
    uint16_t distance_mm;
    uint8_t flags;
} vmc_link_result_t;

typedef struct {
    uint8_t buffer[VMC_LINK_PACKET_SIZE];
    uint8_t length;
    uint32_t good_count;
    uint32_t crc_error_count;
    uint32_t header_error_count;
} vmc_link_parser_t;

void vmc_link_parser_init(vmc_link_parser_t *parser);
uint16_t vmc_link_crc16_ccitt_false(const uint8_t *data, size_t length);
bool vmc_link_feed_byte(
    vmc_link_parser_t *parser,
    uint8_t byte,
    vmc_link_result_t *out
);

#endif
