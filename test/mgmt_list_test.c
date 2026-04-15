/* Test: query kernel MGMT socket for HCI device list */
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <stdint.h>

#define BTPROTO_HCI 1
#define HCI_CHANNEL_CONTROL 3
#define HCI_DEV_NONE 0xffff
#define MGMT_OP_READ_INDEX_LIST 0x0003
#define MGMT_EV_CMD_COMPLETE 0x0001
#define MGMT_INDEX_NONE 0xFFFF

struct sockaddr_hci {
    unsigned short hci_family;
    unsigned short hci_dev;
    unsigned short hci_channel;
};

struct mgmt_pkt {
    uint16_t opcode;
    uint16_t index;
    uint16_t len;
    uint8_t data[1024];
} __attribute__((packed));

int main(void) {
    int fd = socket(PF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
    if (fd < 0) { fprintf(stderr, "socket: %s\n", strerror(errno)); return 1; }

    struct sockaddr_hci addr = {
        .hci_family = 31, /* AF_BLUETOOTH */
        .hci_dev = HCI_DEV_NONE,
        .hci_channel = HCI_CHANNEL_CONTROL,
    };
    if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "bind: %s\n", strerror(errno)); close(fd); return 1;
    }

    struct mgmt_pkt cmd = {
        .opcode = MGMT_OP_READ_INDEX_LIST,
        .index = MGMT_INDEX_NONE,
        .len = 0,
    };
    write(fd, &cmd, 6);

    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    if (poll(&pfd, 1, 3000) > 0) {
        struct mgmt_pkt ev;
        int n = read(fd, &ev, sizeof(ev));
        if (n > 0 && ev.opcode == MGMT_EV_CMD_COMPLETE) {
            uint16_t num = *(uint16_t*)(ev.data + 3);
            fprintf(stderr, "Found %d HCI controllers:", num);
            uint16_t *idx = (uint16_t*)(ev.data + 5);
            for (int i = 0; i < num; i++)
                fprintf(stderr, " hci%d", idx[i]);
            fprintf(stderr, "\n");
        } else {
            fprintf(stderr, "Unexpected response: opcode=%04x len=%d\n", ev.opcode, n);
        }
    } else {
        fprintf(stderr, "No MGMT response (timeout)\n");
    }
    close(fd);
    return 0;
}
