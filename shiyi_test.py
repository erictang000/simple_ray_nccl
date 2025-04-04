#!/usr/bin/env python
import pickle
import numpy as np
# import ray
import sys
import ray

@ray.remote(num_gpus=1)
class NCCLActor:
    def __init__(self, rank, nranks):
        self.rank = rank          # Global rank (0 to nranks-1)
        self.nranks = nranks      # Total number of processes
        # print(f"Rank {rank} master ip: {os.getenv('MASTER_ADDR')}: {os.getenv('MASTER_PORT')}")
        log_versions(rank)

    def get_unique_id(self):
        import cupy.cuda.nccl as nccl
        # Only rank 0 should generate the unique ID.
        if self.rank != 0:
            raise ValueError("Unique ID should only be generated by rank 0")
        # Generate and return the NCCL unique id.
        uid = nccl.get_unique_id()  # returns a tuple
        return uid

    def run(self, unique_id):
        import cupy
        import cupy.cuda.nccl as nccl
        # All actors now receive the unique ID from rank 0.
        cupy.cuda.Device(0).use()
        print(f"Rank {self.rank} using provided unique id.")

        # Create the NCCL communicator.
        print(f"Rank {self.rank} creating NCCL communicator with {self.nranks} total ranks")
        comm = nccl.NcclCommunicator(self.nranks, unique_id, self.rank)
        print(f"Rank {self.rank} DONE creating NCCL communicator")

        # --- Step 1: Broadcast the size of the serialized dictionary ---
        if self.rank == 0:
            my_dict = {"message": "Hello from rank 0", "value": 123}
            serialized = pickle.dumps(my_dict)
            data_size = np.int32(len(serialized))
        else:
            data_size = np.int32(0)
        size_buf = cupy.empty(1, dtype=cupy.int32)
        if self.rank == 0:
            size_buf[0] = data_size
        print(f"Rank {self.rank} about to broadcast int")
        comm.broadcast(size_buf.data.ptr, size_buf.data.ptr, 1, nccl.NCCL_INT32, 0,
                       cupy.cuda.Stream.null.ptr)
        print(f"Rank {self.rank} DONE broadcasting int")
        data_size = int(size_buf.get()[0])

        # --- Step 2: Broadcast the serialized dictionary ---
        if self.rank == 0:
            data_np = np.frombuffer(serialized, dtype=np.uint8)
            assert data_np.size == data_size
            data_buf = cupy.array(data_np)
        else:
            data_buf = cupy.empty(data_size, dtype=cupy.uint8)
        print(f"Rank {self.rank} about to broadcast dict")
        comm.broadcast(data_buf.data.ptr, data_buf.data.ptr, data_buf.size,
                       nccl.NCCL_UINT8, 0, cupy.cuda.Stream.null.ptr)
        print(f"Rank {self.rank} DONE broadcasting dict")

        # --- Step 3: Deserialize the received data ---
        received_bytes = data_buf.get().tobytes()
        received_dict = pickle.loads(received_bytes)
        print("Rank", self.rank, "received dictionary:", received_dict)
        return received_dict

if __name__ == "__main__":
    from ray.util.placement_group import placement_group
    # Initialize Ray (assumes that the Ray cluster is already started)
    # ray.init(
    #     address="auto",
    #     runtime_env={
    #             'env_vars': {
    #                 'NCCL_IB_HCA': 'mlx5_15, mlx5_17',
    #                 'NCCL_DEBUG': 'TRACE',
    #                 'NCCL_DEBUG_SUBSYS': 'NET',
    #                 "VLLM_USE_PRECOMPILED": '1'
    #             },
    #             'py_executable': 'uv run --isolated --directory ./simple_ray_nccl',
    #             "working_dir": "/home/ray/default/"
    #     },
    # )
    total_ranks = 2
    actors = []
    pg = placement_group(bundles=[{'GPU': 1, 'CPU': 1}] * total_ranks, strategy="STRICT_SPREAD")
    for rank in range(total_ranks):
        actor = NCCLActor.options(placement_group=pg).remote(rank, total_ranks)
        actors.append(actor)

    unique_id = ray.get(actors[0].get_unique_id.remote())

    results = ray.get([actor.run.remote(unique_id) for actor in actors])
    print("All results:", results)
