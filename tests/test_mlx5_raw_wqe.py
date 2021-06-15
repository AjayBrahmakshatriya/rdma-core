# SPDX-License-Identifier: (GPL-2.0 OR Linux-OpenIB)
# Copyright (c) 2021 Nvidia, Inc. All rights reserved. See COPYING file

import unittest
import random
import errno


from pyverbs.providers.mlx5.mlx5dv import Mlx5Context, Mlx5DVContextAttr, Mlx5DVQPInitAttr, \
    Mlx5QP, Mlx5DVCQInitAttr, Mlx5CQ, Wqe, WqeDataSeg, WqeCtrlSeg
from pyverbs.pyverbs_error import PyverbsRDMAError, PyverbsUserError, PyverbsError
import pyverbs.providers.mlx5.mlx5_enums as dve
from tests.mlx5_base import Mlx5RDMATestCase
from pyverbs.qp import QPInitAttrEx, QPCap
from pyverbs.cq import CqInitAttrEx
from tests.base import RCResources
from pyverbs.wr import SGE
from pyverbs.mr import MR
import pyverbs.enums as e
import tests.utils as u


class Mlx5RawWqeResources(RCResources):
    def __init__(self, dev_name, ib_port, gid_index):
        self.dv_send_ops_flags = dve.MLX5DV_QP_EX_WITH_RAW_WQE
        self.send_ops_flags = e.IBV_QP_EX_WITH_SEND
        super().__init__(dev_name, ib_port, gid_index)

    def create_context(self):
        mlx5dv_attr = Mlx5DVContextAttr()
        try:
            self.ctx = Mlx5Context(mlx5dv_attr, name=self.dev_name)
        except PyverbsUserError as ex:
            raise unittest.SkipTest(f'Could not open mlx5 context ({ex})')
        except PyverbsRDMAError:
            raise unittest.SkipTest('Opening mlx5 context is not supported')

    def create_qp_init_attr(self):
        comp_mask = e.IBV_QP_INIT_ATTR_PD | e.IBV_QP_INIT_ATTR_SEND_OPS_FLAGS
        return QPInitAttrEx(cap=self.create_qp_cap(), pd=self.pd, scq=self.cq,
                            rcq=self.cq, qp_type=e.IBV_QPT_RC,
                            send_ops_flags=self.send_ops_flags,
                            comp_mask=comp_mask)

    def create_qp_cap(self):
        """
        Create QPCap such that work queue elements will wrap around the send
        work queue, this happens due to the iteration count being higher
        than the max_send_wr.
        :return:
        """
        return QPCap(max_send_wr=4, max_recv_wr=4, max_recv_sge=2, max_send_sge=2)


    def create_qps(self):
        try:
            qp_init_attr = self.create_qp_init_attr()
            comp_mask = dve.MLX5DV_QP_INIT_ATTR_MASK_QP_CREATE_FLAGS | \
                    dve.MLX5DV_QP_INIT_ATTR_MASK_SEND_OPS_FLAGS
            attr = Mlx5DVQPInitAttr(comp_mask=comp_mask, send_ops_flags=self.dv_send_ops_flags)
            qp = Mlx5QP(self.ctx, qp_init_attr, attr)
            self.qps.append(qp)
            self.qps_num.append(qp.qp_num)
            self.psns.append(random.getrandbits(24))
        except PyverbsRDMAError as ex:
            if ex.error_code == errno.EOPNOTSUPP:
                raise unittest.SkipTest('Create Mlx5DV QP is not supported')
            raise ex

    def create_cq(self):
        """
        Initializes self.cq with a dv_cq
        :return: None
        """
        dvcq_init_attr = Mlx5DVCQInitAttr()
        try:
            self.cq = Mlx5CQ(self.ctx, CqInitAttrEx(), dvcq_init_attr)
        except PyverbsRDMAError as ex:
            if ex.error_code == errno.EOPNOTSUPP:
                raise unittest.SkipTest('Create Mlx5DV CQ is not supported')
            raise ex


class RawWqeTest(Mlx5RDMATestCase):
    def setUp(self):
        super().setUp()
        self.iters = 10
        self.server = None
        self.client = None

    def create_players(self, resource, **resource_arg):
        """
        Init RawWqe test resources.
        :param resource: The RDMA resources to use.
        :param resource_arg: Dict of args that specify the resource specific
                             attributes.
        :return: None
        """
        self.client = resource(**self.dev_info, **resource_arg)
        self.server = resource(**self.dev_info, **resource_arg)
        self.client.pre_run(self.server.psns, self.server.qps_num)
        self.server.pre_run(self.client.psns, self.client.qps_num)

    def prepare_send_elements(self):
        mr = self.client.mr
        sge_count = 2
        unit_size = mr.length / 2
        data_segs = [WqeDataSeg(unit_size, mr.lkey, mr.buf + i * unit_size) for
                     i in range(sge_count)]
        ctrl_seg = WqeCtrlSeg()
        ctrl_seg.fm_ce_se = dve.MLX5_WQE_CTRL_CQ_UPDATE
        segment_num = 1 + len(data_segs)
        ctrl_seg.opmod_idx_opcode = dve.MLX5_OPCODE_SEND
        ctrl_seg.qpn_ds = segment_num | int(self.client.qp.qp_num) << 8
        self.raw_send_wqe = Wqe([ctrl_seg] + data_segs)
        self.regular_send_sge = SGE(mr.buf, mr.length, mr.lkey)

    def mixed_traffic(self):
        s_recv_wr = u.get_recv_wr(self.server)
        u.post_recv(self.server, s_recv_wr)
        self.prepare_send_elements()

        for i in range(self.iters):
            self.client.qp.wr_start()
            if i % 2:
                self.client.mr.write('c' * self.client.mr.length, self.client.mr.length)
                self.client.qp.wr_flags = e.IBV_SEND_SIGNALED
                self.client.qp.wr_send()
                self.client.qp.wr_set_sge(self.regular_send_sge)
            else:
                self.client.mr.write('s' * self.client.mr.length, self.client.mr.length)
                self.client.qp.wr_raw_wqe(self.raw_send_wqe)
            self.client.qp.wr_complete()
            u.poll_cq_ex(self.client.cq)
            u.poll_cq_ex(self.server.cq)
            u.post_recv(self.server, s_recv_wr)

            if not i % 2 and self.client.cq.read_opcode() != e.IBV_WC_DRIVER2:
                raise PyverbsError('Opcode validation failed: expected '
                                   f'{e.IBV_WC_DRIVER2}, received {self.client.cq.read_opcode()}')

            act_buffer = self.server.mr.read(self.server.mr.length, 0)
            u.validate(act_buffer, i % 2, self.server.mr.length)

    def test_mixed_raw_wqe_traffic(self):
        """
        Runs traffic with a mix of SEND opcode regular WQEs and SEND opcode RAW
        WQEs.
        """
        self.create_players(Mlx5RawWqeResources)
        self.mixed_traffic()
