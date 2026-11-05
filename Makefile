obj-m := razer-cfg255.o blackctl.o
razer-cfg255-y := kernel/cfg255.o
blackctl-y := kernel/blackctl.o

KERNELDIR ?= /lib/modules/$(shell uname -r)/build

.PHONY: all clean
all:
	$(MAKE) -C $(KERNELDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KERNELDIR) M=$(CURDIR) clean
